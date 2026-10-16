from __future__ import annotations

import asyncio
import base64
import threading
from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError

from llm_gateway.catalogue.model import ModelEntry, get_catalogue
from llm_gateway.catalogue.profiles import profile_errors
from llm_gateway.catalogue.schemas import request_schema
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.ocr import models
from llm_gateway.ocr.models import ImageInfo, OcrRequest, resolve_ocr, validate_image
from llm_gateway.projects.contract import ModelProfile, ProfileParameters, parse_contract
from tests.ocr_fakes import ALIAS, image_data, request
from tests.test_contract import contract_data

pytestmark = pytest.mark.anyio


def req(**options):
    return OcrRequest.model_validate({**request().model_dump(exclude_unset=True), **options})


@pytest.mark.parametrize(
    "options",
    [
        {"model": None},
        {"profile": "reader"},
        {"stream": False},
        {"input": "text"},
        {"temperature": 0},
        {"media_type": "application/pdf"},
        {"media_type": "image/webp"},
        {"data_base64": b"AAAA"},
        {"data_base64": "A" * 10_666_669},
        {"url": "http://private"},
        {"dimensions": 3},
        {"exif_rotation": True},
    ],
)
async def test_strict_ocr_request(options) -> None:
    with pytest.raises(ValidationError):
        req(**options)


@pytest.mark.parametrize(
    "data,media",
    [
        (b"not an image", "image/png"),
        (b"%PDF-1.0", "image/png"),
        (image_data("JPEG"), "image/png"),
        (image_data(), "image/jpeg"),
        (image_data("GIF"), "image/png"),
        (image_data("PNG", (4097, 1)), "image/png"),
        (image_data("PNG", (4000, 3100)), "image/png"),
        (image_data("JPEG")[:-10], "image/jpeg"),
        (b"x" * 8_000_001, "image/png"),
    ],
    ids=lambda _: "image",
)
async def test_invalid_images(data, media) -> None:
    with pytest.raises(CallRejectedError, match="invalid_model_settings"):
        await validate_image(req(data_base64=base64.b64encode(data).decode(), media_type=media))


async def test_corruption_animation_and_base64() -> None:
    raw = bytearray(image_data())
    raw[45] ^= 1
    stream = BytesIO()
    with (
        Image.new("RGB", (10, 10), "white") as first,
        Image.new("RGB", (10, 10), "black") as second,
    ):
        first.save(stream, format="PNG", save_all=True, append_images=[second])
    for encoded in [
        "!!!!",
        "AAAA\n",
        base64.b64encode(raw).decode(),
        base64.b64encode(stream.getvalue()).decode(),
    ]:
        with pytest.raises(CallRejectedError, match="invalid_model_settings"):
            await validate_image(req(data_base64=encoded))


async def test_formats_and_exif_use_original_pixels() -> None:
    for format, media in [("PNG", "image/png"), ("JPEG", "image/jpeg")]:
        exif = Image.Exif()
        exif[274] = 6
        raw = image_data(format, exif=exif)
        info = await validate_image(
            req(media_type=media, data_base64=base64.b64encode(raw).decode())
        )
        assert info == ImageInfo(100, 40, len(raw))


async def test_decode_slot_stays_owned_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    original = Image.open

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "open", hold)
    active = asyncio.create_task(validate_image(req()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        with pytest.raises(CallRejectedError, match="provider_unavailable"):
            await validate_image(req())
    finally:
        release.set()
        for _ in range(100):
            if models._DECODE_SLOT.acquire(blocking=False):
                models._DECODE_SLOT.release()
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("decoder did not release slot")


async def test_catalogue_schema_profiles_and_task_resolution() -> None:
    catalogue = get_catalogue()
    entry = catalogue.models[ALIAS]
    contract = parse_contract(contract_data())
    assert entry.ocr is not None and entry.billing == "zero"
    schema = request_schema(entry)
    assert "input" not in schema["properties"] and schema["properties"]["model"]["const"] == ALIAS
    assert not profile_errors("reader", ModelProfile(model=ALIAS), catalogue)
    assert profile_errors(
        "reader", ModelProfile(model=ALIAS, parameters=ProfileParameters(dimensions=3)), catalogue
    )
    result = resolve_ocr(req(), ImageInfo(100, 40, 100), contract, catalogue)
    assert result.entry == entry
    for options, code in [
        ({"model": "stub-echo"}, "model_task_mismatch"),
        ({"model": "missing"}, "model_not_in_catalogue"),
        ({"model": None, "profile": "missing"}, "profile_not_found"),
    ]:
        with pytest.raises(CallRejectedError, match=code):
            resolve_ocr(req(**options), ImageInfo(100, 40, 100), contract, catalogue)
    for changes in [
        {"schema_version": 3},
        {"context_window_tokens": 1},
        {"streaming": True},
        {"ocr": None},
    ]:
        with pytest.raises(ValidationError):
            ModelEntry.model_validate({**entry.model_dump(), **changes})


def test_schema_three_definition_digests_are_preserved() -> None:
    expected = {
        "local-bge-small-en-v1.5-retrieval-v1": "9676cc1c9b48f9d5b28ba33614f111a4ac0aa00b5778d6f45479471c24d3bb96",
        "local-minilm-l6-v2-v1": "15eac6f0d789266239ceb4bd5b8e0f7ad18d20af38545ceffb652c14cc9b65fb",
        "openai-text-embedding-3-large": "67b7a5311e5a3b5fd0832016fdc9776261ffa7f3af1d54f0fa1f699930a3c08b",
        "openai-text-embedding-3-small": "a48c635151cbf17d59056bb5f23d2ceb981d3aa04fce831645fe16833cc3ef43",
    }
    assert {a: get_catalogue().models[a].definition_digest for a in expected} == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@service",
        "http://service?key=value",
        "http://service#fragment",
        "file:///image",
        "http://service:invalid",
    ],
)
def test_ocr_setting_rejects_nonportable_or_credential_urls(url) -> None:
    from tests.conftest import make_settings

    with pytest.raises(ValidationError):
        make_settings(ocr_base_url=url)
