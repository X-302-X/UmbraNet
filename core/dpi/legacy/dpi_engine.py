"""
UmbraNet - DPI Engine (Локальный обход блокировок)
Реализует модификацию TCP-пакетов для обхода систем глубокой инспекции пакетов (DPI).

Поддерживаемые стратегии:
  - split      : Фрагментация TLS ClientHello на два TCP-сегмента по точке разреза
                 внутри поля SNI. Корректный пересчёт чексумм через WinDivert.
  - fake       : Отправка фейкового TLS-пакета с TTL ≤ hop-count до DPI перед
                 реальным пакетом. DPI видит мусор и сбрасывает состояние,
                 реальный пакет доходит до сервера.
  - split+fake : Комбинация обоих методов (режим combo/dpi_only).
  - disorder   : Отправка второго сегмента раньше первого; сервер собирает по seq,
                 stateful DPI не успевает восстановить порядок.

Режимы запуска (mode):
  'off'    — DPI-движок не активен (режим DNS Only, синий)
  'combo'  — split + fake (режим Combo, чёрный)
  'dpi_only' — split + fake + disorder (режим DPI Only, красный)
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

# WinDivert — опциональная зависимость; на не-Windows или без драйвера
# движок будет недоступен, но не уронит весь процесс.
try:
    import pydivert
except ImportError:
    pydivert = None

log = logging.getLogger("UmbraNet.DPIEngine")

# ── Константы ────────────────────────────────────────────────────────────────

# TTL фейкового пакета: должен быть меньше числа хопов до сервера,
# но больше числа хопов до DPI (обычно DPI стоит на 1-3 хопах от клиента).
FAKE_TTL = 6

# Точка разреза относительно начала SNI hostname (разрезаем посередине имени).
# 0 — разрезаем прямо перед первым байтом hostname (SNI prefix / SNI suffix split).
SNI_SPLIT_OFFSET = 0

# Минимальная длина payload для фрагментации.
MIN_PAYLOAD_LEN = 5

# WinDivert-фильтр: перехватываем исходящий трафик HTTP (TCP 80), HTTPS (TCP 443/8443),
# а также UDP (QUIC 443 и голосовые каналы Discord 50000-50100, 19294-19344).
WINDIVERT_FILTER = (
    "outbound and ("
    "  (tcp and (tcp.DstPort == 443 or tcp.DstPort == 80 or tcp.DstPort == 8443)) or "
    "  (udp and (udp.DstPort == 443 or (udp.DstPort >= 50000 and udp.DstPort <= 50100) or (udp.DstPort >= 19294 and udp.DstPort <= 19344)))"
    ")"
)


# ── TLS-парсер: поиск смещения SNI hostname ──────────────────────────────────

def _find_sni_offset(payload: bytes) -> tuple[int, int] | None:
    """
    Парсит TLS ClientHello и возвращает (offset, length) байтового диапазона
    hostname в SNI-расширении (тип 0x0000).

    Формат (RFC 8446 / RFC 6066):
      TLS Record:
        [0]    Content-Type  = 0x16 (Handshake)
        [1:3]  Version       = 0x03 0x01 (TLS 1.0 record) или 0x03 0x03
        [3:5]  Length        (big-endian)

      Handshake:
        [5]    Handshake-Type = 0x01 (ClientHello)
        [6:9]  Length         (3 байта, big-endian)
        [9:11] Client version
        [11:43] Random (32 байта)
        [43]   Session ID Length
        [44 + sid_len : ...] Cipher Suites, Compression, Extensions...

    Extensions:
        [ext_offset]   Total Extensions Length (2 байта)
        Далее список: [type 2b][len 2b][data len b]...
        SNI extension type = 0x0000
        SNI data: [list_len 2b][name_type 1b][name_len 2b][hostname bytes]
    """
    try:
        if len(payload) < 5:
            return None
        # Проверяем TLS Handshake record
        if payload[0] != 0x16:
            return None
        # ClientHello начинается с байта 5
        if len(payload) < 6 or payload[5] != 0x01:
            return None

        # Пропускаем: HandshakeType(1) + Length(3) + Version(2) + Random(32)
        pos = 5 + 1 + 3 + 2 + 32
        if pos >= len(payload):
            return None

        # Session ID
        sid_len = payload[pos]
        pos += 1 + sid_len
        if pos + 2 > len(payload):
            return None

        # Cipher Suites
        cs_len = struct.unpack("!H", payload[pos:pos + 2])[0]
        pos += 2 + cs_len
        if pos + 1 > len(payload):
            return None

        # Compression Methods
        cm_len = payload[pos]
        pos += 1 + cm_len
        if pos + 2 > len(payload):
            return None

        # Extensions block
        ext_total = struct.unpack("!H", payload[pos:pos + 2])[0]
        pos += 2
        ext_end = pos + ext_total

        # Итерируем расширения
        while pos + 4 <= ext_end and pos + 4 <= len(payload):
            ext_type = struct.unpack("!H", payload[pos:pos + 2])[0]
            ext_len = struct.unpack("!H", payload[pos + 2:pos + 4])[0]
            ext_data_start = pos + 4

            if ext_type == 0x0000:  # server_name
                # SNI data: [list_len 2b][name_type 1b][name_len 2b][hostname]
                if ext_len < 5:
                    return None
                name_type = payload[ext_data_start + 2]
                if name_type != 0x00:  # host_name
                    return None
                name_len = struct.unpack(
                    "!H", payload[ext_data_start + 3: ext_data_start + 5]
                )[0]
                hostname_start = ext_data_start + 5
                if hostname_start + name_len > len(payload):
                    return None
                return hostname_start, name_len

            pos = ext_data_start + ext_len

        return None
    except Exception as exc:
        log.debug("Ошибка парсинга TLS ClientHello: %s", exc)
        return None


# ── Case-flip: точечная мутация только hostname в SNI ────────────────────────

def _mutate_sni(data: bytes) -> bytes:
    """
    Case-flipping: инвертирует регистр ASCII-букв ТОЛЬКО в SNI hostname.

    Правильная реализация парсит структуру TLS ClientHello по RFC 8446
    и находит точное байтовое смещение поля hostname в расширении server_name (0x0000).
    Остальные байты пакета не затрагиваются.

    Если SNI не найден (не ClientHello, нестандартная структура, шифрование) —
    возвращает оригинальный payload без изменений.
    """
    result = _find_sni_offset(data)
    if result is None:
        log.debug("SNI не найден — пакет не мутируется")
        return data

    hostname_start, name_len = result
    buf = bytearray(data)
    for i in range(hostname_start, hostname_start + name_len):
        b = buf[i]
        if 65 <= b <= 90:    # A-Z → a-z
            buf[i] = b + 32
        elif 97 <= b <= 122:  # a-z → A-Z
            buf[i] = b - 32
    log.debug(
        "SNI case-flip: позиция %d, длина %d байт",
        hostname_start, name_len
    )
    return bytes(buf)


def _generate_benign_sni_payload(payload: bytes, hostname_start: int, name_len: int) -> bytes:
    """
    Создаёт payload для фейкового пакета, заменяя заблокированный SNI-хост
    на безопасный (например, google.com или его вариации) ТОЙ ЖЕ длины.
    Это сохраняет все структуры TLS, смещения и длины TCP/IP пакета нетронутыми.
    """
    buf = bytearray(payload)
    benign_base = b"google.com"
    if name_len >= len(benign_base):
        # Дополняем google...aaaa.com до нужной длины
        padding_len = name_len - len(benign_base)
        fake_host = b"google" + b"a" * padding_len + b".com"
    else:
        # Если оригинальный хост слишком короткий, просто заполняем буквами 'a'
        fake_host = b"a" * name_len
    
    buf[hostname_start : hostname_start + name_len] = fake_host
    return bytes(buf)


# ── Точка разреза для фрагментации ───────────────────────────────────────────

def _calc_split_pos(payload: bytes) -> int:
    """
    Вычисляет позицию разреза для TCP-фрагментации.

    Стратегия: разрезаем прямо перед началом SNI hostname.
    Это гарантирует, что первый сегмент не содержит hostname вообще,
    а DPI без полного SNI не может определить домен.

    Если SNI не найден — разрезаем по смещению 3 (после TLS record header),
    что всё равно эффективно против большинства DPI.
    """
    result = _find_sni_offset(payload)
    if result is not None:
        hostname_start, _ = result
        # Разрезаем на байт ДО начала hostname
        split = max(1, hostname_start - 1 + SNI_SPLIT_OFFSET)
        return min(split, len(payload) - 1)
    # Fallback: разрезаем сразу после TLS Record Header (5 байт)
    return min(5, len(payload) - 1)


# ── DPI Engine ────────────────────────────────────────────────────────────────

class DPIEngine:
    """
    Движок перехвата и модификации TCP-пакетов через WinDivert.

    Управляется через dns_server.UmbraNetDNS:
      engine.dpi.start(mode)  — запуск
      engine.dpi.stop()       — остановка

    Список IP для фильтрации синхронизируется с DNS-кэшем ядра,
    а не строится через системный DNS (который может вернуть bogus-IP).
    """

    def __init__(self, engine_ref):
        self.engine = engine_ref   # ссылка на UmbraNetDNS
        self.running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._mode = "off"

        # Кэш IP заблокированных доменов (обновляется из DNS-кэша ядра)
        self.routed_ips: set[str] = set()
        self._last_ips_update: float = 0.0
        self._ips_lock = threading.Lock()

    # ── Обновление списка IP из DNS-кэша ядра ─────────────────────────────

    def _update_routed_ips(self) -> None:
        """
        Собирает IP заблокированных доменов из DNS-кэша основного движка.

        ИСПРАВЛЕНИЕ (дефект #4): ранее использовался socket.getaddrinfo(),
        который обращался к системному DNS провайдера и мог получить bogus-IP.
        Теперь IP берутся из DNSCache ядра — там уже лежат реальные адреса,
        полученные через xbox-dns / DoH / DoT в обход подмены провайдера.

        Fallback на socket.getaddrinfo() срабатывает только если в кэше пусто
        (например, домены ещё не запрашивались).
        """
        now = time.time()
        if now - self._last_ips_update < 60:  # обновляем раз в минуту
            return

        domains: list = self.engine.config.get("routed_domains", [])
        new_ips: set[str] = set()

        # Первичный источник: DNS-кэш ядра (реальные IP, без bogus)
        dns_cache = getattr(self.engine, "cache", None)
        if dns_cache is not None:
            try:
                from dnslib import QTYPE, DNSRecord
                for domain in domains:
                    for qtype_name in ("A", "AAAA"):
                        try:
                            req = DNSRecord.question(domain, qtype_name)
                            # get() возвращает свежий ответ или None
                            resp = dns_cache.get(req, routed=True)
                            if resp is None:
                                # Пробуем stale-ответ — тоже подойдёт для фильтра
                                resp, _ = dns_cache.get_with_state(req, routed=True)
                            if resp is not None:
                                a_code = QTYPE.A
                                aaaa_code = QTYPE.AAAA
                                for rr in resp.rr:
                                    if int(getattr(rr, "rtype", 0)) in (a_code, aaaa_code):
                                        ip_str = str(getattr(rr, "rdata", "")).strip()
                                        if ip_str:
                                            new_ips.add(ip_str)
                        except Exception as exc:
                            log.debug(
                                "Не удалось извлечь IP %s/%s из кэша: %s",
                                domain, qtype_name, exc
                            )
            except Exception as exc:
                log.debug("Ошибка работы с DNS-кэшем: %s", exc)

        # Fallback: если кэш пуст — используем системный резолв как временную меру.
        # Это может вернуть bogus-IP, но лучше, чем пустой фильтр.
        # IP будут перезаписаны при следующем обновлении, когда кэш наполнится.
        if not new_ips:
            log.debug(
                "DNS-кэш пуст для routed_domains, используем системный резолв (временно)"
            )
            for domain in domains:
                try:
                    addr_info = socket.getaddrinfo(domain, None)
                    for item in addr_info:
                        new_ips.add(item[4][0])
                except Exception as exc:
                    log.debug("Не удалось резолвить %s: %s", domain, exc)

        with self._ips_lock:
            self.routed_ips = new_ips
        self._last_ips_update = now
        log.info(
            "DPI IP-фильтр обновлён: %d адресов (источник: %s)",
            len(new_ips),
            "dns-cache" if dns_cache is not None else "system-dns"
        )

    def _is_routed(self, ip: str) -> bool:
        """Проверяет, должен ли пакет обрабатываться DPI."""
        self._update_routed_ips()
        with self._ips_lock:
            return ip in self.routed_ips

    # ── Стратегии обхода DPI ──────────────────────────────────────────────

    def _strategy_split(self, packet) -> None:
        """
        Стратегия Split (фрагментация TCP).

        Разбивает один TLS ClientHello на два TCP-сегмента по точке разреза
        ДО начала SNI hostname. DPI видит первый сегмент без полного SNI
        и не может определить домен для блокировки.
        """
        payload = packet.payload
        if len(payload) < MIN_PAYLOAD_LEN:
            packet.send()
            return

        split_pos = _calc_split_pos(payload)
        log.debug(
            "Split: %d байт → [:%d] + [%d:] для %s",
            len(payload), split_pos, split_pos, packet.dst_addr
        )

        # Первый сегмент: байты до точки разреза
        pkt1 = packet.copy()
        pkt1.payload = payload[:split_pos]
        pkt1.send()

        # Второй сегмент: остаток со скорректированным seq
        pkt2 = packet.copy()
        pkt2.payload = payload[split_pos:]
        pkt2.tcp.seq = packet.tcp.seq + split_pos
        pkt2.send()

    def _strategy_fake(self, packet) -> None:
        """
        Стратегия Fake (десинхронизация через фейковый пакет с малым TTL).

        Алгоритм:
        1. Отправляем фейковый TLS ClientHello с TTL = FAKE_TTL.
           Пакет умрёт по дороге (TTL exhausted), не доходя до сервера.
           DPI видит его первым и обрабатывает как ClientHello.
        2. Следом отправляем реальный ClientHello.
        """
        fake = packet.copy()
        payload = packet.payload
        
        # Пытаемся заменить SNI в фейковом пакете на безопасный (SNI Spoofing)
        sni_info = _find_sni_offset(payload)
        if sni_info is not None:
            hostname_start, name_len = sni_info
            fake_payload = _generate_benign_sni_payload(payload, hostname_start, name_len)
        else:
            fake_payload = bytearray([0x16, 0x03, 0x01, 0x00, 0x28, 0x01]) + b"\xde\xad\xbe\xef" * 10
            fake_payload = bytes(fake_payload)

        fake.payload = fake_payload
        fake.ip.ttl = FAKE_TTL
        fake.send()

        # Реальный пакет отправляем следом
        packet.send()

    def _strategy_disorder(self, packet) -> None:
        """
        Стратегия Disorder (перестановка сегментов).

        Отправляет второй TCP-сегмент раньше первого.
        Сервер корректно собирает пакет по sequence numbers.
        Stateful DPI, ожидающий строгий порядок сегментов, теряет контекст.
        """
        payload = packet.payload
        if len(payload) < MIN_PAYLOAD_LEN:
            packet.send()
            return

        split_pos = _calc_split_pos(payload)

        # Второй сегмент — отправляем первым
        pkt2 = packet.copy()
        pkt2.payload = payload[split_pos:]
        pkt2.tcp.seq = packet.tcp.seq + split_pos
        pkt2.send()

        # Первый сегмент — отправляем вторым
        pkt1 = packet.copy()
        pkt1.payload = payload[:split_pos]
        pkt1.send()

    # ── Диспетчеры стратегий по протоколам ────────────────────────────────

    def _apply_http_strategy(self, packet) -> None:
        """
        Стратегия обхода DPI для HTTP (порт 80).
        - Мутирует регистр заголовка Host: (Host: -> hoSt:)
        - Фрагментирует (сплитит) пакет по первой паре байт.
        """
        payload = packet.payload
        if not payload:
            packet.send()
            return

        # Шаг 1: Case-mixing для заголовка Host:
        mutated = bytearray(payload)
        for idx in range(len(mutated) - 6):
            if mutated[idx:idx+6].lower() == b"host: ":
                mutated[idx:idx+6] = b"hoSt: "
                log.debug("DPI Engine (HTTP): Изменен регистр заголовка Host на 'hoSt:'")
                break
        
        mutated_payload = bytes(mutated)

        # Шаг 2: Фрагментация (Split) на 2 сегмента
        if len(mutated_payload) >= 5:
            split_pos = 2
            try:
                pkt1 = packet.copy()
                pkt1.payload = mutated_payload[:split_pos]
                pkt1.send()

                pkt2 = packet.copy()
                pkt2.payload = mutated_payload[split_pos:]
                pkt2.tcp.seq = packet.tcp.seq + split_pos
                pkt2.send()
                log.debug("DPI Engine (HTTP): Запрос успешно разделен по позиции %d", split_pos)
            except Exception as exc:
                log.error("DPI Engine (HTTP): Сбой split, отправляем исходный: %s", exc)
                try:
                    packet.payload = mutated_payload
                    packet.send()
                except Exception:
                    pass
        else:
            packet.payload = mutated_payload
            packet.send()

    def _apply_https_strategy(self, packet) -> None:
        """
        Стратегия обхода DPI для HTTPS (порт 443, 8443...).

        Стратегии:
          'fake'   — только фейк-пакет (без сплита)
          'split'  — только сплит (фрагментация по порядку, без фейков)
          'combo'  — фейк-пакет + сплит (сбалансированная стратегия)
          'dpi_only' — фейк-пакет + сплит + дисордер (агрессивный обход)
        """
        payload = packet.payload
        if not payload:
            packet.send()
            return

        # Шаг 1: Case-flip только в SNI hostname
        mutated_payload = _mutate_sni(payload)
        split_pos = _calc_split_pos(mutated_payload)

        # Получаем выбранную пользователем стратегию DPI из конфига (с фолбеком на self._mode)
        strategy = self.engine.config.get("dpi_strategy", self._mode)
        if strategy not in ("fake", "split", "combo", "dpi_only"):
            strategy = "dpi_only"

        log.debug("DPI Engine: Применение HTTPS стратегии: %s (режим %s)", strategy, self._mode)

        # Шаг 2: Отправка фейкового пакета с низким TTL для десинхронизации DPI
        if strategy in ("fake", "combo", "dpi_only"):
            try:
                fake_pkt = packet.copy()
                sni_info = _find_sni_offset(payload)
                if sni_info is not None:
                    hostname_start, name_len = sni_info
                    fake_payload = _generate_benign_sni_payload(payload, hostname_start, name_len)
                    log.debug("DPI Engine: Сгенерирован фейк с безопасным SNI длины %d", name_len)
                else:
                    fake_payload = bytearray([0x16, 0x03, 0x01, 0x00, 0x28, 0x01]) + b"\xde\xad\xbe\xef" * 10
                    fake_payload = bytes(fake_payload)
                    log.debug("DPI Engine: Сгенерирован дефолтный фейковый payload")

                fake_pkt.payload = fake_payload
                fake_pkt.ip.ttl = FAKE_TTL
                fake_pkt.send()
                log.debug("DPI Engine: Фейковый пакет (TTL=%d) успешно отправлен", FAKE_TTL)
            except Exception as exc:
                log.error("DPI Engine: Ошибка при отправке фейкового пакета: %s", exc)

        # Шаг 3: Отправка реального мутированного трафика
        if strategy == "fake":
            # Только фейк-пакет (без сплита) — отправляем мутированный реальный пакет целиком
            packet.payload = mutated_payload
            packet.send()

        elif strategy in ("split", "combo"):
            # Обычный сплит (пересылаем два фрагмента по порядку)
            if len(mutated_payload) >= MIN_PAYLOAD_LEN:
                log.debug(
                    "DPI Engine (%s): Отправка split-сегментов: %d байт → [:%d] и [%d:]",
                    strategy, len(mutated_payload), split_pos, split_pos
                )
                try:
                    pkt1 = packet.copy()
                    pkt1.payload = mutated_payload[:split_pos]
                    pkt1.send()

                    pkt2 = packet.copy()
                    pkt2.payload = mutated_payload[split_pos:]
                    pkt2.tcp.seq = packet.tcp.seq + split_pos
                    pkt2.send()
                except Exception as exc:
                    log.error("DPI Engine (%s): Сбой сплита, отправляем исходный пакет: %s", strategy, exc)
                    try:
                        packet.payload = mutated_payload
                        packet.send()
                    except Exception:
                        pass
            else:
                packet.payload = mutated_payload
                packet.send()

        elif strategy == "dpi_only":
            # Дисордер сплит (второй сегмент ПЕРВЫМ, а первый ВТОРЫМ)
            if len(mutated_payload) >= MIN_PAYLOAD_LEN:
                log.debug(
                    "DPI Engine (dpi_only): Отправка out-of-order сегментов: %d байт → [%d:] первым, [:%d] вторым",
                    len(mutated_payload), split_pos, split_pos
                )
                try:
                    # Второй сегмент
                    pkt2 = packet.copy()
                    pkt2.payload = mutated_payload[split_pos:]
                    pkt2.tcp.seq = packet.tcp.seq + split_pos
                    pkt2.send()

                    # Микропауза для надёжности изменения порядка
                    time.sleep(0.002)

                    # Первый сегмент
                    pkt1 = packet.copy()
                    pkt1.payload = mutated_payload[:split_pos]
                    pkt1.send()
                except Exception as exc:
                    log.error("DPI Engine (dpi_only): Сбой disorder, отправляем исходный пакет: %s", exc)
                    try:
                        packet.payload = mutated_payload
                        packet.send()
                    except Exception:
                        pass
            else:
                packet.payload = mutated_payload
                packet.send()

        else:
            # Резервный случай
            packet.payload = mutated_payload
            packet.send()

    def _apply_udp_strategy(self, packet) -> None:
        """
        Стратегия обхода DPI для UDP (QUIC, Discord Voice).
        Отправляет фейковый UDP-пакет с низким TTL, затем реальный.
        """
        payload = packet.payload
        if not payload:
            packet.send()
            return
            
        if self._mode in ("combo", "dpi_only"):
            try:
                # Создаем фейковый UDP-пакет
                fake = packet.copy()
                
                # Payload фейкового UDP пакета: шлем немного случайного мусора той же длины
                # Это собьет парсер DPI, а сервер отбросит фейк из-за невалидности.
                import os
                fake_payload = os.urandom(min(len(payload), 64))
                fake.payload = fake_payload
                
                # Устанавливаем малый TTL, чтобы пакет умер в пути
                fake.ip.ttl = FAKE_TTL
                
                fake.send()
                log.debug("DPI Engine (UDP): Отправлен фейковый UDP пакет (TTL=%d, размер %d)", 
                          FAKE_TTL, len(fake_payload))
            except Exception as exc:
                log.error("DPI Engine (UDP): Ошибка отправки фейка: %s", exc)
                
        # Шлем реальный UDP пакет следом
        packet.send()

    # ── Обработка одного пакета ───────────────────────────────────────────

    def _process_packet(self, packet) -> None:
        """Главная логика обработки перехваченного пакета."""
        # 1. Фильтр: обрабатываем только пакеты к целевым IP
        if not self._is_routed(packet.dst_addr):
            packet.send()
            return

        # 2. Обработка TCP-трафика (HTTP/HTTPS)
        if packet.tcp is not None:
            # 2a. HTTP (порт 80)
            if packet.dst_port == 80:
                self._apply_http_strategy(packet)
                return
            
            # 2b. HTTPS (порт 443, 8443...)
            # Детектируем TLS ClientHello (первый байт payload = 0x16)
            payload = packet.payload
            if payload and payload[0] == 0x16:
                log.debug("DPI Engine: TLS ClientHello перехвачен на TCP %s:%d → %s", 
                          packet.dst_addr, packet.dst_port, self._mode)
                self._apply_https_strategy(packet)
            else:
                packet.send()
                
        # 3. Обработка UDP-трафика (QUIC / Discord Voice)
        elif packet.udp is not None:
            log.debug("DPI Engine: UDP пакет перехвачен на %s:%d → %s", 
                      packet.dst_addr, packet.dst_port, self._mode)
            self._apply_udp_strategy(packet)
            
        else:
            packet.send()

    # ── Жизненный цикл движка ─────────────────────────────────────────────

    def start(self, mode: str) -> bool:
        """
        Запускает DPI-движок в указанном режиме.

        mode: 'combo' (Combo, чёрный) | 'dpi_only' (DPI Only, красный)

        Возвращает True при успешном запуске, False при ошибке
        (WinDivert недоступен, нет прав администратора и т.д.).
        """
        if not pydivert:
            log.error(
                "pydivert не установлен — DPI-движок недоступен. "
                "Установите: pip install pydivert (требует WinDivert драйвер)"
            )
            return False

        if self.running:
            if self._mode == mode:
                return True
            # Режим изменился — перезапускаем
            log.info("Смена режима DPI: %s → %s", self._mode, mode)
            self.stop()

        self._mode = mode
        self.running = True
        self._stop_event.clear()
        # Сбрасываем кэш IP, чтобы сразу обновился при первом пакете
        self._last_ips_update = 0.0

        def _worker() -> None:
            try:
                with pydivert.WinDivert(WINDIVERT_FILTER) as wd:
                    log.info(
                        "DPI-движок запущен (режим: %s). Перехват пакетов...",
                        self._mode
                    )
                    while not self._stop_event.is_set():
                        try:
                            pkt = wd.recv(timeout=1.0)
                            self._process_packet(pkt)
                        except pydivert.exceptions.Timeout:
                            continue
                        except Exception as exc:
                            log.error("Ошибка обработки пакета: %s", exc)
            except Exception as exc:
                log.error("Критическая ошибка DPI-движка: %s", exc)
            finally:
                self.running = False
                log.info("DPI-движок завершил работу")

        self._thread = threading.Thread(
            target=_worker, name="UmbraNet-DPI", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Останавливает DPI-движок и ждёт завершения потока."""
        if not self.running and self._thread is None:
            return
        log.info("Остановка DPI-движка...")
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.running = False
        self._mode = "off"
        log.info("DPI-движок остановлен")
