from pagebound.recipe import DEFAULT_OPTIONS, Recipe


def a_recipe(**overrides) -> Recipe:
    base = {
        "converter": "docling",
        "converter_version": "2.97.0",
        "options": dict(DEFAULT_OPTIONS),
    }
    base.update(overrides)
    return Recipe(**base)


def test_key_is_eight_hex_characters():
    key = a_recipe().key
    assert len(key) == 8
    assert all(c in "0123456789abcdef" for c in key)


def test_the_same_recipe_always_gives_the_same_key():
    assert a_recipe().key == a_recipe().key


def test_option_order_does_not_change_the_key():
    forward = a_recipe(options={"ocr": True, "image_export_mode": "referenced"})
    backward = a_recipe(options={"image_export_mode": "referenced", "ocr": True})
    assert forward.key == backward.key


def test_a_converter_version_bump_changes_the_key():
    # 2.67.0 produced DoclingDocument 1.9.0 with 270 text blocks; 2.97.0
    # produced 1.10.0 with 266 on the same PDF. Different output must not
    # share a cache entry.
    assert a_recipe(converter_version="2.67.0").key != a_recipe(converter_version="2.97.0").key


def test_an_option_change_changes_the_key():
    assert a_recipe(options={"ocr": True}).key != a_recipe(options={"ocr": False}).key


def test_default_options_request_referenced_images():
    assert DEFAULT_OPTIONS["image_export_mode"] == "referenced"
