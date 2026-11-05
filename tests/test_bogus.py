import json
import tempfile
import os
import ipaddress

def test_build_bogus_index_builtin():
    from bogus_ips import build_bogus_index, BUILTIN_BOGUS_IPS
    cfg = {}
    ips, subnets = build_bogus_index(cfg)
    # builtin должен включать все известные заглушки
    assert len(ips) >= len(BUILTIN_BOGUS_IPS) // 2  # минимум половина
    # проверяем что 0.0.0.0 и 212.188.4.10 детектятся
    assert any(str(ip) == "0.0.0.0" for ip in ips)
    assert any(str(ip) == "212.188.4.10" for ip in ips)

def test_response_contains_bogus_positive():
    from bogus_ips import build_bogus_index, response_contains_bogus
    from dnslib import DNSRecord, QTYPE, RR
    from dnslib.dns import A
    cfg = {}
    ips, subnets = build_bogus_index(cfg)
    # строим фейковый DNS ответ с bogus IP
    q = DNSRecord.question("example.com", "A")
    resp = q.reply()
    resp.add_answer(RR("example.com", QTYPE.A, rdata=A("212.188.4.10")))
    is_bogus, matched = response_contains_bogus(resp, ips, subnets)
    assert is_bogus is True
    assert matched == "212.188.4.10"

def test_response_contains_bogus_negative():
    from bogus_ips import build_bogus_index, response_contains_bogus
    from dnslib import DNSRecord, QTYPE, RR
    from dnslib.dns import A
    cfg = {}
    ips, subnets = build_bogus_index(cfg)
    q = DNSRecord.question("example.com", "A")
    resp = q.reply()
    resp.add_answer(RR("example.com", QTYPE.A, rdata=A("1.1.1.1")))
    is_bogus, matched = response_contains_bogus(resp, ips, subnets)
    assert is_bogus is False
    assert matched is None

def test_parse_remote_json_strict_rejects_tiny():
    from bogus_updater import _parse_remote_json
    tiny = json.dumps({"bogus_ips": ["1.2.3.4"], "bogus_subnets": []}).encode()
    try:
        _parse_remote_json(tiny, strict=True)
        assert False, "должен бросить ValueError для tiny"
    except ValueError as e:
        assert "мало" in str(e).lower() or "too" in str(e).lower() or "ожидается" in str(e).lower() or "мало" in str(e)

def test_parse_remote_json_strict_accepts_good():
    from bogus_updater import _parse_remote_json
    # собираем 20+ IP
    ips = [f"10.0.0.{i}" for i in range(1, 22)]
    data = json.dumps({"bogus_ips": ips, "bogus_subnets": ["192.168.0.0/24"]}).encode()
    out_ips, out_subs = _parse_remote_json(data, strict=True)
    assert len(out_ips) == 21
    assert len(out_subs) == 1

def test_parse_remote_json_non_strict_allows_tiny():
    from bogus_updater import _parse_remote_json
    tiny = json.dumps({"bogus_ips": ["1.2.3.4"], "bogus_subnets": []}).encode()
    out_ips, out_subs = _parse_remote_json(tiny, strict=False)
    assert out_ips == ["1.2.3.4"]

def test_save_cache_backup_atomic(tmp_path, monkeypatch):
    from bogus_updater import _save_cache, _cache_path
    import json
    monkeypatch.setenv("UMBRANET_ROLLBACK_ROOT", str(tmp_path / "recovery"))
    with tempfile.TemporaryDirectory() as tmpdir:
        # первая запись
        _save_cache(tmpdir, ["1.1.1.1"], ["10.0.0.0/24"])
        path = _cache_path(tmpdir)
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        assert obj["bogus_ips"] == ["1.1.1.1"]
        # Вторая запись сохраняет прежнюю версию только во внешнем recovery-разделе.
        _save_cache(tmpdir, ["2.2.2.2"], [])
        bak = tmp_path / "recovery" / "network" / "bogus_ips_cache.json.bak"
        assert bak.exists()
        assert not os.path.exists(path + ".bak")
        with open(bak, "r", encoding="utf-8") as f:
            bak_obj = json.load(f)
        assert bak_obj["bogus_ips"] == ["1.1.1.1"]
        with open(path, "r", encoding="utf-8") as f:
            new_obj = json.load(f)
        assert new_obj["bogus_ips"] == ["2.2.2.2"]
