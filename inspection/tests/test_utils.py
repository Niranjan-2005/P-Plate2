from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from inspection.utils import (
    MAX_BATCH_SIZE,
    MAX_FILE_SIZE_BYTES,
    validate_batch_size,
    validate_image_file,
)


def _image_bytes(fmt="PNG", size=(128, 128), color=(20, 80, 160)):
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


class BatchSizeTests(SimpleTestCase):
    def test_accepts_one_to_ten(self):
        self.assertIsNone(validate_batch_size(1))
        self.assertIsNone(validate_batch_size(10))
        self.assertIsNone(validate_batch_size(MAX_BATCH_SIZE))

    def test_rejects_empty_and_oversized(self):
        self.assertIn("between 1 and 10", validate_batch_size(0))
        self.assertIn("received 11", validate_batch_size(11))


class ImageValidationTests(SimpleTestCase):
    def test_valid_png_and_jpeg(self):
        png = SimpleUploadedFile("ok.png", _image_bytes("PNG"), content_type="image/png")
        jpg = SimpleUploadedFile("ok.jpg", _image_bytes("JPEG"), content_type="image/jpeg")
        self.assertIsNone(validate_image_file(png))
        self.assertIsNone(validate_image_file(jpg))

    def test_rejects_not_an_image(self):
        bogus = SimpleUploadedFile("not_an_image.jpg", b"this is not an image", content_type="image/jpeg")
        reason = validate_image_file(bogus)
        self.assertIsNotNone(reason)
        self.assertIn("not a valid decoded JPEG or PNG", reason)

    def test_rejects_too_small(self):
        tiny = SimpleUploadedFile(
            "too_small.png",
            _image_bytes("PNG", size=(32, 32)),
            content_type="image/png",
        )
        reason = validate_image_file(tiny)
        self.assertIn("32×32", reason)
        self.assertIn("64–4096", reason)

    def test_rejects_too_large_file(self):
        payload = b"A" * (MAX_FILE_SIZE_BYTES + 1)
        huge = SimpleUploadedFile("huge.png", payload, content_type="image/png")
        reason = validate_image_file(huge)
        self.assertIn("5 MiB", reason)

    def test_rejects_empty(self):
        empty = SimpleUploadedFile("empty.png", b"", content_type="image/png")
        reason = validate_image_file(empty)
        self.assertIn("Empty", reason)
