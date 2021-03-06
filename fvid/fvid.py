from bitstring import Bits, BitArray
from PIL import Image
import glob
from tqdm import tqdm
import binascii
import argparse
import sys
import os
import getpass
import io
import gzip
import json
import base64

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from Crypto.Cipher import AES

try:
    from fvid_cython import cy_get_bits_from_image as cy_gbfi

    use_cython = True
except (ImportError, ModuleNotFoundError):
    use_cython = False

FRAMES_DIR = "./fvid_frames/"
SALT = (
    "63929291bca3c602de64352a4d4bfe69".encode()
)  # It needs be the same in one instance of coding/decoding
DEFAULT_KEY = " " * 32
DEFAULT_KEY = DEFAULT_KEY.encode()
NOTDEBUG = True
TEMPVIDEO = "_temp.mp4"
FRAMERATE = "1"


class WrongPassword(Exception):
    pass


class MissingArgument(Exception):
    pass


def get_password(password_provided: str) -> bytes:
    """
    Prompt user for password and create a key for decrypting/encrypting

    password_provided: password provided by tge user with -p flag
    """

    if password_provided == "default":
        return DEFAULT_KEY
    else:
        if password_provided == None:
            password_provided = getpass.getpass("Enter password:")

        password = str(password_provided).encode()
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA512(),
            length=32,
            salt=SALT,
            iterations=100000,
            backend=default_backend(),
        )
        key = kdf.derive(password)
        return key


def get_bits_from_file(filepath: str, key: bytes) -> BitArray:
    """
    Get/read bits from file

    filepath -- the file to read
    key -- key used to encrypt file
    """

    print("Reading file...")

    bitarray = BitArray(filename=filepath)

    # adding a delimiter to know when the file ends to avoid corrupted files
    # when retrieving
    cipher = AES.new(key, AES.MODE_EAX, nonce=SALT)
    ciphertext, tag = cipher.encrypt_and_digest(bitarray.tobytes())

    filename = os.path.basename(filepath)

    # because json can only serialize strings, the byte objects are encoded
    # using base64
    data_bytes = json.dumps(
        {
            "tag": base64.b64encode(tag).decode("utf-8"),
            "data": base64.b64encode(ciphertext).decode("utf-8"),
            "filename": filepath,
        }
    ).encode("utf-8")

    print("Zipping...")

    # zip
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="w") as fo:
        fo.write(data_bytes)
    zip = out.getvalue()
    # zip

    del bitarray

    bitarray = BitArray(zip)

    return bitarray.bin


def get_bits_from_image(image: Image) -> str:
    """
    extract bits from image (frame) pixels

    image -- png image file used to extract bits from
    """

    if use_cython:
        bits = cy_gbfi(image)
        return bits

    width, height = image.size

    px = image.load()
    bits = ""

    for y in range(height):
        for x in range(width):

            pixel = px[x, y]

            pixel_bin_rep = "0"

            # if the white difference is smaller, that means the pixel is
            # closer to white, otherwise, the pixel must be black
            if (
                abs(pixel[0] - 255) < abs(pixel[0] - 0)
                and abs(pixel[1] - 255) < abs(pixel[1] - 0)
                and abs(pixel[2] - 255) < abs(pixel[2] - 0)
            ):
                pixel_bin_rep = "1"

            # adding bits
            bits += pixel_bin_rep

    return bits


def get_bits_from_video(video_filepath: str) -> str:
    """
    extract the bits from a video by frame (using a sequence of images)

    video_filepath -- The file path for the video to extract bits from
    """

    print("Reading video...")

    image_sequence = []
    os.system(
        "ffmpeg -i "
        + video_filepath
        + " -c:v libx264rgb -filter:v fps=fps="
        + FRAMERATE
        + " "
        + TEMPVIDEO
    )
    os.system(
        "ffmpeg -i " + TEMPVIDEO + " ./fvid_frames/decoded_frames_%d.png"
    )
    os.remove(TEMPVIDEO)

    for filename in sorted(
        glob.glob(f"{FRAMES_DIR}decoded_frames*.png"), key=os.path.getmtime
    ):
        image_sequence.append(Image.open(filename))

    bits = ""
    sequence_length = len(image_sequence)
    print("Bits are in place")

    if use_cython:
        print("Using Cython...")
    for index in tqdm(range(sequence_length)):
        bits += get_bits_from_image(image_sequence[index])

    return bits


def save_bits_to_file(file_path: str, bits: str, key: bytes):
    """
    save/write bits to a file

    file_path -- the path to write to
    bits -- the bits to write
    key -- key userd for file decryption
    """

    bitstring = Bits(bin=bits)

    # zip
    print("Unziping...")
    in_ = io.BytesIO()
    in_.write(bitstring.bytes)
    in_.seek(0)
    with gzip.GzipFile(fileobj=in_, mode="rb") as fo:
        bitstring = fo.read()
    # zip

    # loading data back from bytes to utf-8 string to deserialize
    data = json.loads(bitstring.decode("utf-8"))

    # decoding previously encoded base64 bytes data to get bytes back
    tag = base64.b64decode(data["tag"])
    ciphertext = base64.b64decode(data["data"])

    filename = data["filename"]

    cipher = AES.new(key, AES.MODE_EAX, nonce=SALT)
    bitstring = cipher.decrypt(ciphertext)

    print("Checking integrity...")

    try:
        cipher.verify(tag)
    except ValueError:
        raise WrongPassword("Key incorrect or message corrupted")

    bitstring = BitArray(bitstring)

    # If filepath not passed in use default otherwise used passed in filepath
    if file_path == None:
        filepath = filename
    else:
        filepath = file_path

    with open(filepath, "wb") as f:
        bitstring.tofile(f)


def split_string_by_n(bitstring: str, n: int) -> list:
    """
    Split a string every n number of characters
    (or less if the 'remaining characters' < n ) this way we can sperate the
    data for an etire video into a list based on the resolution of a frame.

    bitstring -- a string containing bits
    n -- split the string every n characters, for example to split a
    1920 x 1080 frame, this would be 1920*1080 = 2073600
    """

    bit_list = []

    for i in range(0, len(bitstring), n):
        bit_list.append(bitstring[i : i + n])

    return bit_list


def make_image_sequence(bitstring: BitArray, resolution: tuple = (1920, 1080)):
    """
    Create image sequence (frames) for a video

    bitstring -- BitArray of bits used to create pixels with bit data
    resolution -- the resoultion used for each frame (default 1920x1080)
    """

    width, height = resolution

    # split bits into sets of width*height to make (1) image
    set_size = width * height

    # bit_sequence = []
    print("Making image sequence")
    print("Cutting...")

    bitlist = split_string_by_n(bitstring, set_size)

    del bitstring

    bitlist[-1] = bitlist[-1] + "0" * (set_size - len(bitlist[-1]))

    index = 1
    bitlist = bitlist[::-1]
    print("Saving frames...")
    for _ in tqdm(range(len(bitlist))):
        bitl = bitlist.pop()
        image_bits = list(map(int, bitl))
        # print(image_bits)

        image = Image.new("1", (width, height))
        image.putdata(image_bits)
        image.save(f"{FRAMES_DIR}encoded_frames_{index}.png")
        index += 1


def make_video(output_filepath: str, framerate: int = FRAMERATE):
    """
    Create video using ffmpeg

    output_filepath -- the output file path where to store the video
    framerate -- the framerate for the vidoe (default 1)
    """

    if output_filepath == None:
        outputfile = "file.mp4"
    else:
        outputfile = output_filepath

    os.system(
        "ffmpeg -r "
        + framerate
        + " -i ./fvid_frames/encoded_frames_%d.png -c:v libx264rgb "
        + outputfile
    )


def cleanup():
    """
    Clean up the files (frames) creted by fvid during encoding/decoding
    """
    import shutil

    shutil.rmtree(FRAMES_DIR)


def setup():
    """
    setup fvid directory used to store frames for encoding/decoding
    """

    if not os.path.exists(FRAMES_DIR):
        os.makedirs(FRAMES_DIR)


def main():
    global FRAMERATE
    parser = argparse.ArgumentParser(description="save files as videos")
    parser.add_argument(
        "-e", "--encode", help="encode file as video", action="store_true"
    )
    parser.add_argument(
        "-d", "--decode", help="decode file from video", action="store_true"
    )

    parser.add_argument("-i", "--input", help="input file", required=True)
    parser.add_argument("-o", "--output", help="output path")
    parser.add_argument(
        "-f",
        "--framerate",
        help="set framerate for encoding (as a fraction)",
        default=FRAMERATE,
        type=str,
    )
    parser.add_argument(
        "-p",
        "--password",
        help="set password",
        nargs="?",
        type=str,
        default="default",
    )

    args = parser.parse_args()

    setup()

    if not NOTDEBUG:
        print("args", args)
        print(
            "PASSWORD",
            args.password,
            [
                len(args.password) if len(args.password) is not None else None
                for _ in range(0)
            ],
        )

    # using default framerate if none is provided by the user
    if args.framerate != FRAMERATE:
        FRAMERATE = args.framerate

    # check for arguments
    if not args.decode and not args.encode:
        raise MissingArgument("You should use either --encode or --decode!")

    key = get_password(args.password)

    if args.decode:
        bits = get_bits_from_video(args.input)

        file_path = None

        if args.output:
            file_path = args.output

        save_bits_to_file(file_path, bits, key)

    elif args.encode:

        # isdigit has the benefit of being True and raising an error if the
        # user passes a negative string
        # all() lets us check if both the negative sign and forward slash are
        # in the string, to prevent negative fractions
        if (not args.framerate.isdigit() and "/" not in args.framerate) or all(
            x in args.framerate for x in ("-", "/")
        ):
            raise NotImplementedError(
                "The framerate must be a positive fraction or an integer for "
                "now, like 3, '1/3', or '1/5'!"
            )

        # get bits from file
        bits = get_bits_from_file(args.input, key)

        # create image sequence
        make_image_sequence(bits)

        video_file_path = None

        if args.output:
            video_file_path = args.output

        make_video(video_file_path, args.framerate)

    cleanup()