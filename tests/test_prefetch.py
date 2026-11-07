"""No real challenges, credentials, network or browser; synthetic independent images."""
import io
import random
from unittest.mock import Mock

from PIL import Image
import pytest
import requests

from captcha import solver
from captcha.prefetch import PrefetchedSolver, captcha_url, fingerprint, match_tiles, MAX_IMAGES


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Prefetch unit tests cannot access the network")
    monkeypatch.setattr(requests.sessions.Session, "request", denied)


@pytest.fixture
def images():
    return [Image.frombytes("RGB", (64, 64), random.Random(i).randbytes(64 * 64 * 3)) for i in range(18)]


def add_image(cache, image, number=0):
    data = io.BytesIO()
    image.save(data, format="PNG")
    request = Mock(url=f"https://imgs.hcaptcha.com/fixture-private-url/{number}")
    response = Mock(request=request, url=request.url, status=200,
                    headers={"content-type": "image/png"}, body=lambda: data.getvalue())
    cache.observe_request(request)
    cache.observe_response(response)


def classify(wanted):
    def solve(header, images, model, **kwargs):
        return [i + 1 for i, image in enumerate(images) if fingerprint(image) in wanted]
    return Mock(side_effect=solve)


def test_two_rounds_use_one_batch_with_pixel_order_mapping(images):
    wanted = {fingerprint(images[i]) for i in (0, 4, 10, 17)}
    model = classify(wanted)
    transport = Mock()
    cache = PrefetchedSolver(transport, model)
    page = Mock()
    cache.attach(page)
    for i, image in enumerate(images):
        add_image(cache, image, i)
    header = Image.new("RGB", (200, 100), "blue")
    assert cache(header, images[:9], "test", api_key="fixture-key") == [1, 5]
    assert cache(header, images[9:], "test", api_key="fixture-key") == [2, 9]
    model.assert_called_once()
    assert len(model.call_args.args[1]) == 18
    assert model.call_args.kwargs["batch"] is True
    assert model.call_args.kwargs["session"] is transport
    assert cache.statistics()["cache_hits"] == 1
    assert "fixture-private-url" not in repr(cache.statistics())
    cache.close()
    assert not cache.images and not cache.predictions and not cache.pending
    assert page.remove_listener.call_count == 2
    assert cache.statistics()["model_calls"] == 1


@pytest.mark.parametrize("change", ["instruction", "reference"])
def test_changed_task_falls_back_instead_of_reusing_predictions(images, change):
    model = classify({fingerprint(images[0]), fingerprint(images[10])})
    cache = PrefetchedSolver(Mock(), model)
    for i, image in enumerate(images):
        add_image(cache, image, i)
    header = Image.new("RGB", (200, 100), "blue")
    reference = Image.new("RGB", (64, 64), "green")
    cache(header, images[:9], "test", ref_img=reference)
    if change == "instruction":
        header = Image.new("RGB", (200, 100), "red")
    else:
        reference = Image.new("RGB", (64, 64), "red")
    assert cache(header, images[9:], "test", ref_img=reference) == [2]
    assert model.call_count == 2
    assert not model.call_args.kwargs.get("batch", False)
    assert len(model.call_args.args[1]) == 9
    assert cache.statistics()["fallback_reasons"] == {"instruction_changed": 1}


def test_missing_assets_and_duplicate_mapping_use_original_round_solver(images):
    model = classify({fingerprint(images[0])})
    cache = PrefetchedSolver(Mock(), model)
    header = Image.new("RGB", (200, 100), "blue")
    assert cache(header, images[:9], "test") == [1]
    assert not model.call_args.kwargs.get("batch", False)
    for i, image in enumerate(images[:9]):
        add_image(cache, image, i)
    assert cache(header, images[:9], "test") == [1]
    assert cache.statistics()["fallback_reasons"]["pool_incomplete"] == 1
    assert match_tiles([images[0]] * 9, images) is None


def test_new_generation_discards_old_and_inflight_assets(images):
    cache = PrefetchedSolver(Mock())
    add_image(cache, images[0])
    request = Mock(url="https://imgs.hcaptcha.com/old-response")
    cache.observe_request(request)
    cache.observe_request(Mock(url="https://api.hcaptcha.com/getcaptcha/fixture-private-id"))
    assert not cache.images
    response = Mock(request=request, url=request.url, status=200,
                    headers={"content-type": "image/png"})
    cache.observe_response(response)
    response.body.assert_not_called()
    assert not cache.images
    assert "fixture-private" not in repr(cache.statistics())


def test_mixed_asset_dimensions_are_not_batched(images):
    images[0] = images[0].resize((32, 32))
    model = classify({fingerprint(images[0])})
    cache = PrefetchedSolver(Mock(), model)
    for index, image in enumerate(images):
        add_image(cache, image, index)
    assert cache(Image.new("RGB", (200, 100)), images[:9], "test") == [1]
    assert not model.call_args.kwargs.get("batch", False)


def test_generation_change_during_body_read_is_ignored(images):
    cache = PrefetchedSolver(Mock())
    request = Mock(url="https://imgs.hcaptcha.com/old-response")
    cache.observe_request(request)
    data = io.BytesIO()
    images[0].save(data, format="PNG")
    def body():
        cache.observe_request(Mock(url="https://api.hcaptcha.com/getcaptcha/new"))
        return data.getvalue()
    cache.observe_response(Mock(request=request, url=request.url, status=200,
                               headers={"content-type": "image/png"}, body=body))
    assert not cache.images


def test_generation_change_after_model_answer_never_reuses_answer(images):
    model = classify(set())
    cache = PrefetchedSolver(Mock(), model)
    page = Mock()
    cache.attach(page)
    for i, image in enumerate(images):
        add_image(cache, image, i)
    page.wait_for_timeout.side_effect = lambda _: cache.observe_request(
        Mock(url="https://api.hcaptcha.com/getcaptcha/new"))
    with pytest.raises(RuntimeError, match="captcha_images_changed"):
        cache(Image.new("RGB", (200, 100)), images[:9], "test")
    assert not cache.predictions
    model.assert_called_once()


@pytest.mark.parametrize("url", ["http://hcaptcha.com/x", "https://hcaptcha.com.evil.invalid/x",
    "https://sso.acesso.gov.br/login", "https://private@hcaptcha.com/x", "https://hcaptcha.com:444/x"])
def test_untrusted_urls_are_not_image_sources(url):
    assert not captcha_url(url)


def test_image_cache_has_limits_and_ignores_invalid_payloads(images):
    cache = PrefetchedSolver(Mock())
    request = Mock(url="https://imgs.hcaptcha.com/image")
    cache.observe_request(request)
    response = Mock(request=request, url=request.url, status=200,
                    headers={"content-type": "image/png", "content-length": "2000001"})
    cache.observe_response(response)
    response.body.assert_not_called()
    add_image(cache, Image.new("RGB", (513, 513)))
    assert not cache.images
    for i in range(MAX_IMAGES + 2):
        add_image(cache, Image.new("RGB", (64, 64), (i, i, i)), i)
    assert len(cache.images) == MAX_IMAGES


def test_batch_model_error_is_not_silently_retried(images):
    model = Mock(side_effect=requests.Timeout("fixture-private-provider-error"))
    cache = PrefetchedSolver(Mock(), model)
    for i, image in enumerate(images):
        add_image(cache, image, i)
    with pytest.raises(requests.Timeout):
        cache(Image.new("RGB", (200, 100)), images[:9], "test")
    model.assert_called_once()
    assert cache.statistics()["fallback_calls"] == 0


def test_batch_prompt_numbers_all_images_and_parses_high_indices():
    image = Image.new("RGB", (2, 2))
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": "[1,12,18]"}}]}
    transport = Mock()
    transport.post.return_value = response
    assert solver.solve(image, [image] * 18, "test", api_key="fixture-key",
                        session=transport, batch=True) == [1, 12, 18]
    content = transport.post.call_args.kwargs["json"]["messages"][0]["content"]
    assert len([part for part in content if part["type"] == "image_url"]) == 19
    response.close.assert_called_once()
    response.json.return_value = {"choices": [{"message": {"content": "[19]"}}]}
    with pytest.raises(ValueError, match="invalid_batch_indices"):
        solver.solve(image, [image] * 18, "test", api_key="fixture-key", session=transport, batch=True)
