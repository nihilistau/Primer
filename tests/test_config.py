import pytest

from primer.config import ConfigError, list_tiers, load_tier, read_settings, write_settings


def test_load_hybrid_tier():
    t = load_tier("hybrid")
    assert t.text.kind in ("openai", "sp") and t.fast.kind in ("openai", "sp")
    assert t.source_mode == "storyteller" and t.gpu_mode == "auto"


def test_fast_may_reuse_text(tmp_path):
    (tmp_path / "solo.toml").write_text('name="solo"\nfast="text"\n[text]\nkind="openai"\nbase_url="http://127.0.0.1:1234"\n')
    t = load_tier("solo", tiers_dir=tmp_path)
    assert t.fast == t.text


def test_unknown_tier_and_bad_kind(tmp_path):
    with pytest.raises(ConfigError):
        load_tier("nope", tiers_dir=tmp_path)
    (tmp_path / "bad.toml").write_text('name="bad"\nfast="text"\n[text]\nkind="grpc"\nbase_url="x"\n')
    with pytest.raises(ConfigError, match="kind"):
        load_tier("bad", tiers_dir=tmp_path)


def test_settings_roundtrip(tmp_path):
    assert read_settings(tmp_path) == {}
    write_settings(tmp_path, {"tier": "light"})
    assert read_settings(tmp_path) == {"tier": "light"}


def test_shipped_tiers_all_load():
    assert set(list_tiers()) == {"light", "hybrid", "workstation", "hosted"}
    for n in list_tiers():
        load_tier(n)


def test_stop_command_validated(tmp_path):
    (tmp_path / "s.toml").write_text('fast="text"\n[text]\nkind="openai"\nbase_url="http://x"\nstop=["lms","server","stop"]\n')
    assert load_tier("s", tiers_dir=tmp_path).text.stop == ("lms", "server", "stop")
    (tmp_path / "b.toml").write_text('fast="text"\n[text]\nkind="openai"\nbase_url="http://x"\nstop="lms"\n')
    with pytest.raises(ConfigError, match="stop"):
        load_tier("b", tiers_dir=tmp_path)


def test_lm_studio_tiers_have_stop_command():
    for n in ("hybrid", "workstation"):
        assert load_tier(n).text.stop == ("lms", "server", "stop")


@pytest.mark.parametrize("name", ["../tiers/hybrid", r"..\outside", "../outside", "C:/x/evil",
                                  "//host/share/evil", "hy.brid", "", "a/b", None, 3])
def test_load_tier_rejects_names_that_are_not_plain_tier_names(tmp_path, name):
    tiers = tmp_path / "tiers"
    tiers.mkdir()
    (tmp_path / "outside.toml").write_text('fast="text"\n[text]\nkind="openai"\nbase_url="http://x"\n')
    with pytest.raises(ConfigError):
        load_tier(name, tiers_dir=tiers)


def test_load_tier_rejects_absolute_path_to_real_file(tmp_path):
    tiers = tmp_path / "tiers"
    tiers.mkdir()
    evil = tmp_path / "evil.toml"
    evil.write_text('fast="text"\n[text]\nkind="openai"\nbase_url="http://x"\n')
    with pytest.raises(ConfigError):
        load_tier(str(evil)[:-5], tiers_dir=tiers)
    with pytest.raises(ConfigError):
        load_tier(str(evil)[:-5].replace("\\", "/"), tiers_dir=tiers)


_TEXT = 'fast="text"\n[text]\nkind="openai"\nbase_url="http://x"\n'


def test_gpu_flag_parsed_and_defaults_false(tmp_path):
    (tmp_path / "a.toml").write_text(_TEXT)
    assert load_tier("a", tiers_dir=tmp_path).text.gpu is False
    (tmp_path / "b.toml").write_text(_TEXT + "gpu=true\n")
    assert load_tier("b", tiers_dir=tmp_path).text.gpu is True


def test_gpu_must_be_bool(tmp_path):
    (tmp_path / "c.toml").write_text(_TEXT + 'gpu="yes"\n')
    with pytest.raises(ConfigError, match="gpu"):
        load_tier("c", tiers_dir=tmp_path)


def test_sdserver_kind_and_health_path():
    from primer.config import EngineSpec
    spec = EngineSpec(kind="sdserver", base_url="http://x")
    assert spec.health_path == "/sdcpp/v1/models/status"


def test_load_tier_rejects_sdserver_as_text_or_fast(tmp_path):
    (tmp_path / "d.toml").write_text('fast="text"\n[text]\nkind="sdserver"\nbase_url="http://x"\n')
    with pytest.raises(ConfigError, match=r'd\.text: kind "sdserver" is not a text engine'):
        load_tier("d", tiers_dir=tmp_path)
    (tmp_path / "e.toml").write_text('[text]\nkind="openai"\nbase_url="http://x"\n'
                                     '[fast]\nkind="sdserver"\nbase_url="http://y"\n')
    with pytest.raises(ConfigError, match=r'e\.fast: kind "sdserver" is not a text engine.*\[images\]'):
        load_tier("e", tiers_dir=tmp_path)


def test_make_engine_rejects_sdserver():
    from primer.config import EngineSpec
    from primer.engines import EngineError, make_engine
    with pytest.raises(EngineError, match="sdserver is not a text engine"):
        make_engine(EngineSpec(kind="sdserver", base_url="http://x"))


def test_images_table_parsed(tmp_path):
    (tmp_path / "i.toml").write_text('[images]\nsd_server="C:/x/sd-server.exe"\n' + _TEXT.replace('fast="text"\n', ''), encoding="utf-8")
    # top-level keys must precede tables; rebuild in valid order
    (tmp_path / "i.toml").write_text(_TEXT + '[images]\nsd_server="C:/x/sd-server.exe"\n')
    img = load_tier("i", tiers_dir=tmp_path).images
    assert img.sd_server == "C:/x/sd-server.exe" and img.port == 8820 and img.ready_timeout_s == 900.0
    (tmp_path / "j.toml").write_text(_TEXT + '[images]\nsd_server="s"\nport=18901\nready_timeout_s=30\n')
    img = load_tier("j", tiers_dir=tmp_path).images
    assert img.port == 18901 and img.ready_timeout_s == 30.0


def test_images_absent_is_none(tmp_path):
    (tmp_path / "n.toml").write_text(_TEXT)
    assert load_tier("n", tiers_dir=tmp_path).images is None


@pytest.mark.parametrize("images", [
    'sd_server=3', 'sd_server=""', 'port=8820',
    'sd_server="s"\nport="8820"', 'sd_server="s"\nport=80', 'sd_server="s"\nport=70000',
    'sd_server="s"\nport=true', 'sd_server="s"\nready_timeout_s="x"',
])
def test_images_rejects_non_string_path_and_bad_port(tmp_path, images):
    (tmp_path / "k.toml").write_text(_TEXT + "[images]\n" + images + "\n")
    with pytest.raises(ConfigError, match="images"):
        load_tier("k", tiers_dir=tmp_path)


def test_shipped_tiers_still_load():
    assert load_tier("hybrid").text.gpu is True
    assert load_tier("workstation").text.gpu is True
    for n in list_tiers():
        t = load_tier(n)
        assert t.images is None  # the [images] example in the shipped tiers stays commented


def test_images_stall_timeout_default_and_parsed(tmp_path):
    (tmp_path / "s.toml").write_text(_TEXT + '[images]\nsd_server="s"\n')
    assert load_tier("s", tiers_dir=tmp_path).images.stall_timeout_s == 1800.0
    (tmp_path / "t.toml").write_text(_TEXT + '[images]\nsd_server="s"\nstall_timeout_s=600\n')
    assert load_tier("t", tiers_dir=tmp_path).images.stall_timeout_s == 600.0


@pytest.mark.parametrize("value", ['"x"', "0", "-5", "nan", "inf", "true"])
def test_images_rejects_bad_stall_timeout(tmp_path, value):
    (tmp_path / "u.toml").write_text(_TEXT + f'[images]\nsd_server="s"\nstall_timeout_s={value}\n')
    with pytest.raises(ConfigError, match="stall_timeout_s"):
        load_tier("u", tiers_dir=tmp_path)


def test_pause_command_parsed_and_validated(tmp_path):
    (tmp_path / "p.toml").write_text(_TEXT + 'pause=["lms","unload","--all"]\n')
    assert load_tier("p", tiers_dir=tmp_path).text.pause == ("lms", "unload", "--all")
    (tmp_path / "q.toml").write_text(_TEXT)
    assert load_tier("q", tiers_dir=tmp_path).text.pause == ()
    (tmp_path / "r.toml").write_text(_TEXT + 'pause="lms unload"\n')
    with pytest.raises(ConfigError, match="pause"):
        load_tier("r", tiers_dir=tmp_path)


def test_lm_studio_tiers_unload_models_to_pause():
    for n in ("hybrid", "workstation"):
        t = load_tier(n).text
        assert t.gpu and t.launch == ("lms", "server", "start")
        assert t.pause == ("lms", "unload", "--all")
