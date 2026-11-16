from ytop.fsnames import numbered_stem, plan_names, safe_name


def test_safe_name_strips_forbidden():
    assert safe_name('a:b*c?"d<e>f|g/h\\i') == "a b c d e f g h i"
    assert safe_name("Gödel, Escher") == "Gödel, Escher"
    assert safe_name("name. ") == "name"
    assert safe_name("CON") == "CON_"
    assert safe_name("") == "Untitled"
    assert len(safe_name("x" * 300)) == 100


def test_safe_name_reserved_and_truncation():
    assert safe_name("nul") == "nul_"
    assert safe_name("COM1") == "COM1_"
    assert safe_name("con.txt") == "con_.txt"
    assert safe_name("CONSOLE") == "CONSOLE"
    assert not safe_name("a" * 9 + ". " + "b" * 50, limit=10).endswith((" ", "."))


def test_numbered_stem_width():
    assert numbered_stem(3, 9, "Intro") == "03 - Intro"
    assert numbered_stem(3, 150, "Intro") == "003 - Intro"
    assert len(numbered_stem(1, 9, "x" * 300)) <= 100


def test_plan_names_collision():
    assert plan_names([("a", "Same"), ("b", "same")], numbered=False) == {
        "a": "Same",
        "b": "same [b]",
    }


def test_plan_names_numbered():
    assert plan_names([("a", "X"), ("b", "Y")], numbered=True) == {
        "a": "01 - X",
        "b": "02 - Y",
    }


def test_safe_name_truncation_never_lands_on_reserved():
    from ytop.fsnames import _RESERVED

    for value, limit in [("CONSOLE", 3), ("COM1 port", 4), ("NULL.txt", 3),
                         ("AUXILIARY", 3), ("LPT12", 4)]:
        out = safe_name(value, limit=limit)
        assert len(out) <= limit
        assert out.partition(".")[0].strip().upper() not in _RESERVED, out


def test_plan_names_collision_suffix_fits_limit():
    long_title = "x" * 150
    names = plan_names([("aaaaaaaaaaa", long_title), ("bbbbbbbbbbb", long_title)], False)
    assert all(len(n) <= 100 for n in names.values())
    assert names["bbbbbbbbbbb"].endswith(" [bbbbbbbbbbb]")
    numbered = plan_names([("a1", "Same"), ("b2", "Same")], True)
    assert numbered["a1"] != numbered["b2"]
