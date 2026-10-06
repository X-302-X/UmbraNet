"""
UmbraNet Strategy Manager

Новая модель DPI:
  • routed_domains из главного меню = единый список целей;
  • JSON-стратегия Uz1/Uz2/... = только метод обхода (args winws.exe);
  • remote_hostlists больше не используются.
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("UmbraNet.StrategyManager")

# Discord-картинки (аватарки, вложения, превью) сидят на Cloudflare CDN.
# Агрессивный fake+multisplit, который открывает discord.com / gateway, на
# CDN ломает HTTP/2 и HTTP/3 — чат живой, квадраты вместо картинок.
# Эти хосты выносим в отдельную мягкую секцию winws.
_DISCORD_CDN_SUFFIXES = (
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net",
    "discordapp.net",
    "discordapp.com",
    "discordcdn.com",
    "discord.media",
    "dl.discordapp.net",
    "stable.dl2.discordapp.net",
    "discord-attachments-uploads-prd.storage.googleapis.com",
)


def is_discord_cdn_host(domain: str) -> bool:
    d = str(domain or "").strip().lower().strip(".")
    if not d:
        return False
    for suffix in _DISCORD_CDN_SUFFIXES:
        if d == suffix or d.endswith("." + suffix):
            return True
    return False


def partition_discord_cdn(domains: list[str]) -> tuple[list[str], list[str]]:
    """Делит цели на «обычные» и Discord CDN (аватарки/картинки)."""
    main: list[str] = []
    cdn: list[str] = []
    for d in domains or []:
        (cdn if is_discord_cdn_host(d) else main).append(d)
    return main, cdn


def _discord_cdn_section(cdn_hostlist_arg: str) -> list[str]:
    """Мягкий обход Cloudflare CDN + быстрый срыв QUIC (клиент падает на TCP)."""
    return [
        "--filter-tcp=443",
        cdn_hostlist_arg,
        "--dpi-desync=fake,split2",
        "--dpi-desync-split-pos=1",
        # ts — проверенный fooling всех рабочих секций (md5sig флакал:
        # картинки грузились через раз, поле 2026-10-05).
        "--dpi-desync-fooling=ts",
        "--dpi-desync-repeats=6",
        "--new",
        "--filter-udp=443",
        cdn_hostlist_arg,
        "--dpi-desync=fake",
        "--dpi-desync-any-protocol=1",
        "--dpi-desync-cutoff=n2",
        "--dpi-desync-repeats=11",
        "--dpi-desync-fake-quic={bin}\\quic_initial_www_google_com.bin",
        "--new",
    ]


def _has_discord(domains: list[str]) -> bool:
    """Есть ли среди целей Discord (галочка Discord в главном меню)."""
    return any("discord" in d.lower() for d in domains)


def _drop_voice_sections(args: list[str]) -> list[str]:
    """Убирает ГОЛОСОВЫЕ секции (fake-stun / UDP-порты голоса).

    Работает только при включённом Discord (решение 2026-10-05): нет
    галочки — секции нет, голосовые фейки не шлются. Секция определяется
    по --dpi-desync-fake-stun или --filter-udp с портами, кроме 443
    (443 — QUIC, он не голос).
    """

    def is_voice(sec: list[str]) -> bool:
        for a in sec:
            if a.startswith("--dpi-desync-fake-stun") or a.startswith("--dpi-desync-fake-discord"):
                return True
            if a.startswith("--filter-udp="):
                for p in a.split("=", 1)[1].split(","):
                    lo = p.strip().split("-")[0]
                    if lo.isdigit() and lo != "443":
                        return True
        return False

    sections: list[list[str]] = [[]]
    for a in args:
        if a == "--new":
            sections.append([])
        else:
            sections[-1].append(a)
    kept = [sec for sec in sections if not is_voice(sec)]
    out: list[str] = []
    for i, sec in enumerate(kept):
        if i:
            out.append("--new")
        out.extend(sec)
    return out


def _inject_after_wf(args: list[str], extra: list[str]) -> list[str]:
    i = 0
    while i < len(args) and str(args[i]).startswith("--wf-"):
        i += 1
    return args[:i] + extra + args[i:]


class StrategyManager:
    def __init__(self, strategies_dir=None):
        if strategies_dir is None:
            current = Path(__file__).resolve().parent
            found = False
            for _ in range(6):
                candidate = current / "strategies"
                if candidate.exists():
                    self.strategies_dir = candidate.resolve()
                    found = True
                    break
                current = current.parent
            if not found:
                self.strategies_dir = (Path(__file__).resolve().parents[2] / "strategies").resolve()
        else:
            self.strategies_dir = Path(str(strategies_dir)).expanduser().resolve()
        self.strategies_dir.mkdir(parents=True, exist_ok=True)
        self.active_hostlist_path = self.strategies_dir / "active_routed_hostlist.txt"
        self.cdn_hostlist_path = self.strategies_dir / "active_discord_cdn_hostlist.txt"
        self.last_error = ""
        self.last_hostlist_count = 0

    @staticmethod
    def _clean_domains(lines) -> list[str]:
        out = []
        seen = set()
        for raw in lines or []:
            line = str(raw).strip().lower()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("||")
            line = line.strip("^*/ ")
            for prefix in ("https://", "http://"):
                line = line.removeprefix(prefix)
            line = line.split("/")[0].strip().strip(".")
            if not line or "." not in line:
                continue
            if line not in seen:
                seen.add(line)
                out.append(line)
        return out

    def _load_json(self, path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Не удалось прочитать стратегию %s: %s", path.name, exc)
            return None

    def list_strategies(self, enabled_only=True):
        strategies = []
        for f in sorted(self.strategies_dir.glob("*.json")):
            data = self._load_json(f)
            if isinstance(data, dict) and "id" in data and "name" in data:
                if not enabled_only or data.get("enabled", True):
                    args = list(data.get("args", []) or []) if isinstance(data.get("args", []), list) else []
                    strategies.append({
                        "id": str(data["id"]),
                        "name": str(data["name"]),
                        "description": str(data.get("description", "")),
                        "args": args,
                        "hostlist": [],
                    })
        return strategies

    def get_strategy(self, strategy_id: str):
        sid = str(strategy_id or "").strip().lower()
        for f in self.strategies_dir.glob("*.json"):
            data = self._load_json(f)
            if isinstance(data, dict) and str(data.get("id", "")).lower() == sid:
                return data
        return None

    def _write_hostlist_file(self, path: Path, domains: list[str]) -> str:
        if not domains:
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
            return ""
        content = "\n".join(domains) + "\n"
        try:
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            if old != content:
                path.write_text(content, encoding="utf-8")
        except Exception as exc:
            self.last_error = f"Не удалось записать hostlist {path.name}: {exc}"
            log.error(self.last_error)
            return ""
        return f"--hostlist={path.absolute()}"

    # ECH-обложка Chrome: при шифрованном ClientHello DPI видит именно это
    # имя (поле 2026-10-06: 49 рукопожатий за прогон). Без него ECH-потоки не
    # получают обход и зависают — «сайт вечно грузится» (YT Music).
    ECH_COVER_DOMAIN = "cloudflare-ech.com"

    def _routed_with_cover(self, domains: list[str]) -> list[str]:
        """Основной список целей + служебная обложка ECH (без неё ECH-потоки
        не получают обход и зависают). В счётчик целей не входит."""
        return list(domains) + [self.ECH_COVER_DOMAIN] if domains else []

    def _write_active_hostlist(self, routed_domains) -> tuple[str, int]:
        domains = self._clean_domains(routed_domains or [])
        self.last_hostlist_count = len(domains)
        arg = self._write_hostlist_file(
            self.active_hostlist_path,
            self._routed_with_cover(domains),
        )
        return arg, (len(domains) if arg or not domains else 0)

    def get_args(self, strategy_id: str, routed_domains=None, require_hostlist: bool = False):
        """Возвращает args для winws.exe.

        routed_domains — список целей из главного меню. Если он передан, WinWS
        ограничивается dynamic hostlist: active_routed_hostlist.txt.
        """
        self.last_error = ""
        self.last_hostlist_count = 0
        strategy = self.get_strategy(strategy_id)
        if not strategy:
            self.last_error = f"Стратегия '{strategy_id}' не найдена"
            log.error(self.last_error)
            return []

        sid = str(strategy.get("id") or strategy_id)
        raw_args = strategy.get("args", []) or []
        if not isinstance(raw_args, list):
            self.last_error = f"Стратегия '{sid}': поле args должно быть списком"
            log.error(self.last_error)
            return []
        args = [str(a).strip() for a in raw_args if str(a).strip()]
        args = [a.replace("--dpi-desync=split2,fake", "--dpi-desync=fake,split2") for a in args]
        if not args:
            self.last_error = f"Стратегия '{sid}' пока пустая. Заполните args для WinWS."
            log.warning(self.last_error)
            return []

        hostlist_arg = ""
        cdn_arg = ""
        if routed_domains is not None:
            domains = self._clean_domains(routed_domains or [])
            self.last_hostlist_count = len(domains)
            main, cdn = partition_discord_cdn(domains)
            # Режем только когда есть И обычные цели, И CDN: иначе некуда
            # деть discord.com / gateway, либо наоборот — один список как раньше.
            if main and cdn:
                hostlist_arg = self._write_hostlist_file(
                    self.active_hostlist_path, self._routed_with_cover(main)
                )
                cdn_arg = self._write_hostlist_file(self.cdn_hostlist_path, cdn)
            else:
                hostlist_arg = self._write_hostlist_file(
                    self.active_hostlist_path, self._routed_with_cover(domains)
                )
                self._write_hostlist_file(self.cdn_hostlist_path, [])
            if require_hostlist and not hostlist_arg:
                self.last_error = "Для DPI не выбраны цели: включите сервисы/домены в главном меню."
                log.warning(self.last_error)
                return []
            # Голосовые секции — только при включённом Discord (2026-10-05).
            if not _has_discord(domains):
                args = _drop_voice_sections(args)
                if not args:
                    self.last_error = f"Стратегия '{sid}': после удаления голосовой секции не осталось аргументов"
                    log.warning(self.last_error)
                    return []

        if "{hostlist}" in " ".join(args):
            if not hostlist_arg:
                self.last_error = f"Стратегия '{sid}' требует hostlist, но список целей пуст"
                log.warning(self.last_error)
                return []
            args = [arg.replace("{hostlist}", hostlist_arg) for arg in args]
        elif hostlist_arg:
            # Hostlist применяем к каждой секции winws (--new начинает новую секцию).
            new_args = [hostlist_arg]
            for arg in args:
                new_args.append(arg)
                if arg == "--new":
                    new_args.append(hostlist_arg)
            args = new_args

        if cdn_arg:
            args = _inject_after_wf(args, _discord_cdn_section(cdn_arg))

        bin_dir = self.strategies_dir.parent / "bin"
        lists_dir = self.strategies_dir.parent / "lists"
        final_args = []
        for arg in args:
            arg = arg.replace("{bin}", str(bin_dir.absolute()))
            arg = arg.replace("{lists}", str(lists_dir.absolute()))
            final_args.append(arg)

        unresolved = [a for a in final_args if "{" in a or "}" in a]
        if unresolved:
            self.last_error = f"Стратегия '{sid}': неразрешённые плейсхолдеры: {unresolved[:3]}"
            log.error(self.last_error)
            return []
        return final_args

    def validate_all(self) -> list[dict]:
        rows = []
        for item in self.list_strategies(enabled_only=False):
            sid = item["id"]
            args = self.get_args(sid, routed_domains=["example.com"], require_hostlist=False)
            rows.append({
                "id": sid,
                "ok": bool(args),
                "args_count": len(args),
                "error": "" if args else self.last_error,
            })
        return rows


_manager = None


def get_strategy_manager():
    global _manager
    if _manager is None:
        _manager = StrategyManager()
    return _manager
