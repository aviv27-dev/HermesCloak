from hermescloak.entities import StaticFileSource, CallableSource

def test_static_file_source(tmp_path):
    p = tmp_path / "names.txt"
    p.write_text("# comment\nשירה לוי\tלקוח\nדני כהן\tלקוח\nבית המשפט\tמוסד\n", encoding="utf-8")
    src = StaticFileSource(str(p))
    assert ("שירה לוי", "לקוח") in list(src.names())
    assert ("בית המשפט", "מוסד") in list(src.names())
    assert len(list(src.names())) == 3

def test_static_file_source_default_type(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("פלוני אלמוני\n", encoding="utf-8")
    src = StaticFileSource(str(p), default_type="לקוח")
    assert list(src.names()) == [("פלוני אלמוני", "לקוח")]

def test_callable_source():
    src = CallableSource(lambda: [("X", "לקוח")])
    assert list(src.names()) == [("X", "לקוח")]
