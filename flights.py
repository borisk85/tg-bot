"""
Поиск авиабилетов через Travelpayouts API.
Адаптировано из agent-builder-saas для standalone использования в tg-bot.
"""
import json
import os
import logging
import httpx

logger = logging.getLogger(__name__)

def _load_airlines_db() -> dict:
    try:
        db_path = os.path.join(os.path.dirname(__file__), "airlines_db.json")
        with open(db_path, encoding="utf-8") as f:
            data = json.load(f)
        return {item["code"]: item["name"] for item in data if item.get("code") and item.get("name")}
    except Exception:
        return {}

_AIRLINES_DB: dict = _load_airlines_db()

CITY_TO_IATA: dict = {
    "алматы": "ALA", "алма-ата": "ALA", "almaty": "ALA",
    "астана": "NQZ", "нур-султан": "NQZ", "nur-sultan": "NQZ",
    "шымкент": "CIT", "атырау": "GUW", "актау": "SCO",
    "актобе": "AKX", "костанай": "KSN", "павлодар": "PWQ",
    "усть-каменогорск": "UKK", "семей": "SEM",
    "тараз": "DMB", "жамбыл": "DMB",
    "уральск": "URA", "орал": "URA",
    "кызылорда": "KZO", "петропавловск": "PPK",
    "карагандa": "KGF", "туркестан": "HSA",
    "москва": "MOW", "moscow": "MOW",
    "санкт-петербург": "LED", "питер": "LED", "спб": "LED",
    "новосибирск": "OVB", "екатеринбург": "SVX", "сочи": "AER",
    "краснодар": "KRR", "казань": "KZN", "уфа": "UFA",
    "самара": "KUF", "ростов-на-дону": "ROV", "тюмень": "TJM",
    "иркутск": "IKT", "красноярск": "KJA",
    "владивосток": "VVO", "хабаровск": "KHV",
    "калининград": "KGD", "нижний новгород": "GOJ",
    "пермь": "PEE", "челябинск": "CEK", "омск": "OMS",
    "воронеж": "VOZ", "якутск": "YKS", "барнаул": "BAX",
    "стамбул": "IST", "istanbul": "IST",
    "анталья": "AYT", "анталия": "AYT",
    "бодрум": "BJV", "даламан": "DLM", "измир": "ADB",
    "дубай": "DXB", "dubai": "DXB",
    "абу-даби": "AUH", "шарджа": "SHJ",
    "бангкок": "BKK", "bangkok": "BKK",
    "паттайя": "UTP", "утапао": "UTP",
    "пхукет": "HKT", "самуи": "USM",
    "краби": "KBV", "чианграй": "CEI",
    "тбилиси": "TBS", "батуми": "BUS",
    "ереван": "EVN", "баку": "GYD",
    "ташкент": "TAS", "самарканд": "SKD",
    "бишкек": "FRU", "ош": "OSS",
    "минск": "MSQ",
    "душанбе": "DYU", "худжанд": "LBD",
    "ашхабад": "ASB", "ашгабат": "ASB",
    "киев": "KBP", "київ": "KBP", "kyiv": "KBP",
    "кишинев": "KIV",
    "тель-авив": "TLV", "tel aviv": "TLV",
    "амман": "AMM", "бейрут": "BEY", "маскат": "MCT",
    "кувейт": "KWI", "доха": "DOH", "doha": "DOH",
    "лондон": "LON", "london": "LON",
    "париж": "PAR", "paris": "PAR",
    "берлин": "BER", "амстердам": "AMS",
    "барселона": "BCN", "мадрид": "MAD",
    "рим": "ROM", "милан": "MIL",
    "вена": "VIE", "прага": "PRG",
    "варшава": "WAW", "стокгольм": "STO",
    "рига": "RIX", "таллин": "TLL", "вильнюс": "VNO",
    "белград": "BEG", "подгорица": "TGD", "тиват": "TIV",
    "будапешт": "BUD", "бухарест": "OTP", "софия": "SOF",
    "токио": "TYO", "tokyo": "TYO",
    "сеул": "SEL", "seoul": "SEL",
    "сингапур": "SIN", "singapore": "SIN",
    "бали": "DPS", "денпасар": "DPS",
    "мальдивы": "MLE", "мале": "MLE",
    "сейшелы": "SEZ", "маэ": "SEZ",
    "пунта-кана": "PUJ", "пунта кана": "PUJ",
    "доминикана": "SDQ",
    "гонконг": "HKG", "hong kong": "HKG",
    "куала-лумпур": "KUL", "куалалумпур": "KUL",
    "хошимин": "SGN", "сайгон": "SGN",
    "ханой": "HAN", "hanoi": "HAN",
    "нячанг": "CXR", "камрань": "CXR",
    "да нанг": "DAD", "дананг": "DAD",
    "фукуок": "PQC",
    "манила": "MNL", "джакарта": "CGK",
    "чиангмай": "CNX",
    "каир": "CAI", "шарм-эль-шейх": "SSH", "хургада": "HRG",
    "буэнос-айрес": "BUE", "мехико": "MEX", "лима": "LIM",
    "нью-йорк": "NYC", "new york": "NYC",
    "лос-анджелес": "LAX", "los angeles": "LAX",
    "чикаго": "CHI", "chicago": "CHI",
    "майами": "MIA", "miami": "MIA",
    "сан-франциско": "SFO", "лас-вегас": "LAS",
    "торонто": "YTO", "ванкувер": "YVR",
    "сидней": "SYD", "sydney": "SYD",
    "мельбурн": "MEL", "melbourne": "MEL",
    "ларнака": "LCA", "афины": "ATH",
    "занзибар": "ZNZ", "маврикий": "MRU",
    "тенерифе": "TFS", "родос": "RHO", "ираклион": "HER",
    "дубровник": "DBV", "сплит": "SPU",
}

AIRLINE_NAMES: dict = {
    "KC": "Air Astana", "FS": "FlyArystan", "DV": "SCAT", "ZK": "Qazaq Air",
    "TK": "Turkish Airlines", "PC": "Pegasus",
    "FZ": "FlyDubai", "EK": "Emirates", "G9": "Air Arabia",
    "SU": "Aeroflot", "S7": "S7 Airlines", "DP": "Pobeda",
    "U6": "Ural Airlines", "FV": "Rossiya", "5N": "Smartavia",
    "N4": "Nordwind", "J2": "Azerbaijan Airlines",
    "QR": "Qatar Airways", "EY": "Etihad", "GF": "Gulf Air",
    "HY": "Uzbekistan Airways", "B2": "Belavia",
    "LH": "Lufthansa", "BA": "British Airways",
    "AF": "Air France", "KL": "KLM", "AY": "Finnair",
    "OS": "Austrian", "LX": "Swiss", "SK": "SAS",
    "IB": "Iberia", "VY": "Vueling", "FR": "Ryanair",
    "U2": "EasyJet", "W6": "Wizz Air",
    "OZ": "Asiana Airlines", "KE": "Korean Air", "CX": "Cathay Pacific",
    "NH": "ANA", "JL": "Japan Airlines", "MH": "Malaysia Airlines",
    "SQ": "Singapore Airlines", "TG": "Thai Airways",
    "AI": "Air India", "6E": "IndiGo",
    "YK": "Avia Traffic", "ZY": "Sky Kyrgyzstan",
}

AIRLINE_ALIASES: dict = {
    "turkish airlines": "TK", "turkish": "TK", "турецкие": "TK", "туркиш": "TK",
    "air astana": "KC", "эйр астана": "KC", "астана": "KC",
    "flydubai": "FZ", "флайдубай": "FZ",
    "emirates": "EK", "эмирейтс": "EK",
    "aeroflot": "SU", "аэрофлот": "SU",
    "s7": "S7", "с7": "S7", "сибирь": "S7",
    "pobeda": "DP", "победа": "DP",
    "qatar": "QR", "катар": "QR", "qatar airways": "QR",
    "pegasus": "PC", "пегасус": "PC",
    "air arabia": "G9", "эйр арабия": "G9",
    "etihad": "EY", "этихад": "EY",
    "lufthansa": "LH", "люфтганза": "LH",
    "british airways": "BA", "бритиш": "BA",
    "klm": "KL", "клм": "KL",
    "air france": "AF", "эйр франс": "AF",
    "uzbekistan airways": "HY", "узбекистан": "HY",
    "belavia": "B2", "белавиа": "B2",
    "scat": "DV", "скат": "DV",
    "qazaq air": "ZK", "казак эйр": "ZK",
    "ural airlines": "U6", "уральские": "U6",
    "wizz": "W6", "виз": "W6",
}


def _get_iata(city: str):
    city = city.strip()
    if len(city) == 3 and city.upper().isalpha():
        return city.upper()
    c = city.lower()
    if c in CITY_TO_IATA:
        return CITY_TO_IATA[c]
    for key, code in CITY_TO_IATA.items():
        if c in key or key in c:
            return code
    return None


def _resolve_airline_code(airline_str: str):
    s = airline_str.strip().lower()
    if len(s) == 2 and s.upper() in AIRLINE_NAMES:
        return s.upper()
    for alias, code in AIRLINE_ALIASES.items():
        if alias in s or s in alias:
            return code
    for code, name in AIRLINE_NAMES.items():
        if s in name.lower() or name.lower() in s:
            return code
    return None


def _departure_hour(dt_str: str) -> int:
    try:
        return int(dt_str[11:13])
    except Exception:
        return -1


def _matches_time_period(hour: int, period: str) -> bool:
    if hour < 0:
        return True
    p = period.lower().strip()
    if p in ("утро", "утром", "утренний", "утренние", "morning"):
        return 6 <= hour < 12
    if p in ("день", "днём", "дневной", "дневные", "afternoon", "обед"):
        return 12 <= hour < 18
    if p in ("вечер", "вечером", "вечерний", "вечерние", "evening"):
        return 18 <= hour < 22
    if p in ("ночь", "ночью", "ночной", "ночные", "night"):
        return hour >= 22 or hour < 6
    return True


def _fmt_duration(minutes: int) -> str:
    if not minutes:
        return ""
    h = minutes // 60
    m = minutes % 60
    return f"{h}ч {m}м" if m else f"{h}ч"


def _fmt_dt(dt_str: str):
    months = ["янв", "фев", "мар", "апр", "мая", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек"]
    try:
        date_part = dt_str[:10]
        time_part = dt_str[11:16]
        y, mo, d = date_part.split("-")
        return f"{int(d)} {months[int(mo)-1]}", time_part
    except Exception:
        return dt_str[:10], ""


def _time_label(hour: int) -> str:
    if 6 <= hour < 12:
        return "утро"
    if 12 <= hour < 18:
        return "день"
    if 18 <= hour < 22:
        return "вечер"
    if hour >= 22 or hour < 6:
        return "ночь"
    return ""


class FlightsModule:
    URL_CALENDAR = "https://api.travelpayouts.com/v1/prices/calendar"
    URL_V2 = "https://api.travelpayouts.com/v2/prices/latest"
    URL_V1 = "https://api.travelpayouts.com/v1/prices/cheap"

    async def search(self, origin, destination, month, max_price=None,
                     direct_only=False, airline=None, departure_time=None,
                     max_duration_hours=None, day_from=None, day_to=None,
                     round_trip=False, return_month=None) -> str:

        if round_trip:
            import re as _re
            _DISC = "ℹ️ Цены выше являются ориентировочными. Точную стоимость авиаперелета необходимо смотреть напрямую на сайте Aviasales."

            def _split(result):
                if result.startswith("FLIGHTS_BTN:"):
                    rest = result[len("FLIGHTS_BTN:"):]
                    idx = rest.find("\n")
                    if idx != -1:
                        return rest[:idx].strip(), rest[idx+1:].strip().replace(_DISC, "").rstrip()
                return "", result.replace(_DISC, "").rstrip()

            def _min_price(text):
                prices = _re.findall(r'<b>\$(\d+)</b>', text)
                return min(int(p) for p in prices) if prices else None

            def _earliest_day(text):
                days = _re.findall(r'\b(\d{1,2})\s+(?:янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек)\b', text)
                return min(int(d) for d in days) if days else None

            outbound = await self.search(origin, destination, month, max_price, direct_only, airline, departure_time, max_duration_hours, day_from, day_to)
            out_url, out_text = _split(outbound)

            eff_return_month = return_month or month
            if return_month and return_month != month:
                ret_day_from = None
            else:
                earliest = _earliest_day(out_text)
                ret_day_from = (earliest + 1) if earliest else day_from

            inbound = await self.search(destination, origin, eff_return_month, max_price, direct_only, airline, departure_time, max_duration_hours, ret_day_from, day_to)
            _, in_text = _split(inbound)

            out_min = _min_price(out_text)
            in_min = _min_price(in_text)

            def _strip_plane(t): return t[len("✈️ "):] if t.startswith("✈️ ") else t

            combined = f"🛫 <b>ТУДА:</b>\n{_strip_plane(out_text)}\n\n— — — — — — — — — —\n\n🛬 <b>ОБРАТНО:</b>\n{_strip_plane(in_text)}"
            if out_min and in_min:
                combined += f"\n\n<b>Туда-обратно от ${out_min + in_min}</b>"
            combined += f"\n\n{_DISC}"
            return f"FLIGHTS_BTN:{out_url}\n{combined}"

        token = os.getenv("TRAVELPAYOUTS_TOKEN", "")
        if not token:
            return "Модуль авиабилетов временно недоступен — TRAVELPAYOUTS_TOKEN не задан."

        origin_iata = _get_iata(origin)
        dest_iata = _get_iata(destination)
        if not origin_iata:
            return f"Не знаю такой город: «{origin}». Напиши полное название — например, Алматы, Москва, Стамбул, Дубай."
        if not dest_iata:
            return f"Не знаю такой город: «{destination}». Напиши полное название — например, Алматы, Москва, Стамбул, Дубай."

        airline_code = _resolve_airline_code(airline) if airline else None

        import asyncio as _asyncio
        calendar_flights, v2_flights, v1_flights = await _asyncio.gather(
            self._fetch_calendar(origin_iata, dest_iata, month, token),
            self._fetch_v2(origin_iata, dest_iata, month, token),
            self._fetch_v1(origin_iata, dest_iata, month, token),
        )

        all_direct_durs = [f["duration"] for f in (v1_flights + v2_flights)
                           if f.get("transfers", 0) == 0 and f.get("duration") and f["duration"] > 0]
        typical_direct_duration = min(all_direct_durs) if all_direct_durs else 0

        v2_by_date: dict = {}
        for f in v2_flights:
            date = f["departure_at"][:10]
            if date not in v2_by_date or f["price"] < v2_by_date[date]["price"]:
                v2_by_date[date] = f

        v1_direct_dates = {f["departure_at"][:10] for f in v1_flights if f.get("transfers", 0) == 0}
        v1_has_direct = any(f.get("transfers", 0) == 0 for f in v1_flights) if v1_flights else True

        flights = []
        if calendar_flights:
            for flight in calendar_flights:
                date = flight["departure_at"][:10]
                v2 = v2_by_date.get(date)
                if v2 and not flight.get("duration"):
                    v2_dur = v2.get("duration", 0)
                    same_type = v2.get("transfers", 0) == flight.get("transfers", 0)
                    not_anomaly = not typical_direct_duration or v2_dur <= typical_direct_duration * 1.5
                    if same_type and not_anomaly and v2_dur:
                        flight["duration"] = v2_dur
                if not v1_has_direct and flight.get("transfers", 0) == 0:
                    v2_transfers = v2.get("transfers", 0) if v2 else 0
                    flight["transfers"] = min(max(v2_transfers, 1), 2)
                elif flight.get("transfers", 0) > 0 and date in v1_direct_dates:
                    flight["transfers"] = 0
                if not flight.get("duration") and flight.get("transfers", 0) == 0 and typical_direct_duration:
                    flight["duration"] = typical_direct_duration
            flights = calendar_flights

            direct_durs = sorted([f["duration"] for f in flights
                                   if f.get("transfers", 0) == 0 and f.get("duration") and f["duration"] > 0])
            if len(direct_durs) >= 3:
                median_dur = direct_durs[len(direct_durs) // 2]
                for f in flights:
                    if f.get("transfers", 0) == 0 and f.get("duration", 0) > median_dur * 1.5:
                        f["duration"] = median_dur
            if typical_direct_duration:
                for f in flights:
                    if f.get("transfers", 0) > 0 and f.get("duration", 0) <= typical_direct_duration:
                        f["duration"] = 0
        elif v2_flights:
            flights = v2_flights
        elif v1_flights:
            flights = v1_flights

        if not flights:
            return f"Рейсов {origin_iata} → {dest_iata} в {month} не найдено.\nПопробуй другой месяц или соседние аэропорты."

        filtered = list(flights)
        if max_price:
            filtered = [f for f in filtered if f["price"] <= max_price]
        if direct_only:
            filtered = [f for f in filtered if f["transfers"] == 0]
        if airline_code:
            filtered = [f for f in filtered if f["airline"] == airline_code]
        if departure_time:
            filtered = [f for f in filtered if _matches_time_period(f["departure_hour"], departure_time)]
        if max_duration_hours:
            max_min = max_duration_hours * 60
            filtered = [f for f in filtered if not f["duration"] or f["duration"] <= max_min]
        if day_from or day_to:
            def _day_of(f):
                try:
                    dep = f.get("departure_at", "")
                    return int(dep[8:10]) if len(dep) >= 10 else 0
                except (ValueError, TypeError):
                    return 0
            if day_from:
                filtered = [f for f in filtered if _day_of(f) >= day_from]
            if day_to:
                filtered = [f for f in filtered if _day_of(f) <= day_to]

        if not direct_only and not airline and not departure_time and not max_price and not max_duration_hours and not day_from and not day_to:
            direct_results = [f for f in filtered if f["transfers"] == 0]
            if direct_results:
                filtered = direct_results

        marker = os.getenv("TRAVELPAYOUTS_MARKER", "")

        if not filtered:
            cheapest = min(flights, key=lambda x: x["price"])
            c_airline = AIRLINE_NAMES.get(cheapest["airline"]) or _AIRLINES_DB.get(cheapest["airline"], cheapest["airline"])
            c_transfers = "прямой" if cheapest["transfers"] == 0 else ("1 пересадка" if cheapest["transfers"] == 1 else f"{cheapest['transfers']} пересадки")
            c_date, c_time = _fmt_dt(cheapest.get("departure_at", ""))
            c_dur = _fmt_duration(cheapest.get("duration", 0))
            hints = []
            if max_price: hints.append(f"до ${max_price:.0f}")
            if direct_only: hints.append("прямой")
            if airline: hints.append(airline)
            if departure_time: hints.append(f"вылет: {departure_time}")
            if max_duration_hours: hints.append(f"до {max_duration_hours}ч")
            not_found_line = f"Рейсов {airline} на маршруте {origin_iata} → {dest_iata} в {month} не найдено." if (airline and airline_code) else f"По запросу ({', '.join(hints)}) рейсов не найдено."
            msg = f"{not_found_line}\n\nЛучший вариант на этот маршрут:\n<b>${cheapest['price']}</b> · {c_airline} · {c_transfers}"
            if c_date:
                msg += f"\n{c_date}"
                if c_time: msg += f" · вылет {c_time}"
            if c_dur: msg += f" · {c_dur} в пути"
            cheapest_has_stops = cheapest["transfers"] > 0
            if max_duration_hours: msg += "\n\n💡 Попробуй увеличить максимальное время в пути."
            elif max_price and direct_only:
                msg += "\n\n💡 Попробуй увеличить бюджет." if cheapest_has_stops else "\n\n💡 Попробуй увеличить бюджет или поискать рейсы с пересадкой."
            elif max_price: msg += "\n\n💡 Попробуй увеличить бюджет."
            elif direct_only and not cheapest_has_stops: msg += "\n\n💡 Прямых рейсов на этом маршруте в этом месяце нет."
            elif departure_time: msg += "\n\n💡 Попробуй другое время вылета."
            try:
                cheapest_dep = cheapest.get("departure_at", "")
                lm = f"{cheapest_dep[8:10]}{cheapest_dep[5:7]}"
                if not lm.isdigit() or len(lm) != 4: raise ValueError
            except Exception:
                try: _, m_part = month.split("-"); lm = f"01{m_part}"
                except Exception: lm = ""
            _link = f"https://www.aviasales.com/search/{origin_iata}{lm}{dest_iata}1"
            if marker: _link += f"?marker={marker}"
            return f"FLIGHTS_BTN:{_link}\n{msg}"

        filtered.sort(key=lambda x: (x["price"], x.get("duration") or 9999))
        top = filtered[:5]

        try:
            cheapest_dep = top[0].get("departure_at", "")
            link_date = f"{cheapest_dep[8:10]}{cheapest_dep[5:7]}"
            if not link_date.isdigit() or len(link_date) != 4: raise ValueError
        except Exception:
            try: _, m = month.split("-"); link_date = f"01{m}"
            except Exception: link_date = ""

        link = f"https://www.aviasales.com/search/{origin_iata}{link_date}{dest_iata}1"
        if marker: link += f"?marker={marker}"

        filter_parts = []
        if direct_only: filter_parts.append("прямые")
        if departure_time: filter_parts.append(departure_time)
        if max_price: filter_parts.append(f"до ${max_price:.0f}")
        if airline: filter_parts.append(airline)
        if max_duration_hours: filter_parts.append(f"до {max_duration_hours}ч")
        if day_from and day_to: filter_parts.append(f"{day_from}-{day_to} числа")
        elif day_from: filter_parts.append(f"с {day_from} числа")
        elif day_to: filter_parts.append(f"по {day_to} числа")

        all_direct = all(f["transfers"] == 0 for f in filtered)
        header = f"✈️ {origin_iata} → {dest_iata}, {month}"
        active_filters = list(filter_parts)
        if all_direct and not direct_only: active_filters.insert(0, "прямые")
        if active_filters: header += f" ({', '.join(active_filters)})"

        n = len(filtered)
        if n % 10 == 1 and n % 100 != 11: var_word = "вариант"
        elif 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): var_word = "варианта"
        else: var_word = "вариантов"

        header += f"\nНашел {n} {var_word}, показываю топ {len(top)}:\n" if len(top) < len(filtered) else f"\nНашел {n} {var_word}:\n"

        lines = [header]
        for i, f in enumerate(top, 1):
            airline_name = AIRLINE_NAMES.get(f["airline"]) or _AIRLINES_DB.get(f["airline"], f["airline"])
            transfers_str = "прямой ✅" if f["transfers"] == 0 else ("1 пересадка" if f["transfers"] == 1 else f"{f['transfers']} пересадки")
            date_str, time_str = _fmt_dt(f.get("departure_at", ""))
            dur_str = _fmt_duration(f.get("duration", 0))
            hour = f.get("departure_hour", -1)
            time_label = _time_label(hour) if hour >= 0 else ""
            prefix = "" if len(top) == 1 else f"{i}. "
            parts = [f"{prefix}{date_str}" if date_str else prefix.strip()]
            if time_str: parts.append(f"{time_str} ({time_label})" if time_label else time_str)
            if airline_name: parts.append(airline_name)
            parts.append(f"<b>${f['price']}</b>")
            lines.append(" · ".join(parts))
            details = [transfers_str]
            if dur_str: details.append(f"в пути {dur_str}")
            lines.append(f"   {' · '.join(details)}")
            lines.append("")

        lines.append("ℹ️ Цены выше являются ориентировочными. Точную стоимость авиаперелета необходимо смотреть напрямую на сайте Aviasales.")

        if len(filtered) <= 2 and (max_price or direct_only or max_duration_hours):
            hints = []
            if max_price: hints.append("увеличить бюджет")
            if direct_only: hints.append("поискать рейсы с пересадкой")
            if max_duration_hours: hints.append("увеличить максимальное время в пути")
            lines.append(f"\nХочешь больше вариантов? Попробуй {' или '.join(hints)}.")

        return f"FLIGHTS_BTN:{link}\n" + "\n".join(lines)

    async def _fetch_calendar(self, origin, destination, month, token) -> list:
        params = {"origin": origin, "destination": destination, "depart_date": month, "currency": "usd", "token": token}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self.URL_CALENDAR, params=params)
        except Exception as e:
            logger.error(f"Flights calendar error: {e}")
            return []
        if resp.status_code != 200: return []
        data = resp.json()
        if not data.get("success") or not data.get("data"): return []
        flights = []
        for date_key, info in data["data"].items():
            if not isinstance(info, dict): continue
            if month and not date_key.startswith(month): continue
            departure_dt = info.get("departure_at", date_key)
            flights.append({
                "airline": info.get("airline", ""), "price": info.get("price", 0),
                "transfers": info.get("number_of_changes", 0), "departure_at": departure_dt,
                "departure_hour": _departure_hour(departure_dt), "duration": info.get("duration", 0),
            })
        return flights

    async def _fetch_v2(self, origin, destination, month, token) -> list:
        params = {"origin": origin, "destination": destination, "currency": "usd", "period_type": "month",
                  "one_way": "true", "limit": 100, "show_to_affiliates": "true", "sorting": "price",
                  "trip_class": 0, "token": token}
        if month: params["beginning_of_period"] = month + "-01"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self.URL_V2, params=params)
        except Exception as e:
            logger.error(f"Flights v2 error: {e}")
            return []
        if resp.status_code != 200: return []
        data = resp.json()
        if not data.get("success") or not data.get("data"): return []
        flights = []
        for item in data["data"]:
            departure_dt = item.get("departure_at", item.get("depart_date", ""))
            flights.append({
                "airline": item.get("airline", ""),
                "price": item.get("value", item.get("price", 0)),
                "transfers": item.get("number_of_changes", item.get("transfers", 0)),
                "departure_at": departure_dt, "departure_hour": _departure_hour(departure_dt),
                "duration": item.get("duration", 0) or item.get("duration_to", 0),
            })
        return flights

    async def _fetch_v1(self, origin, destination, month, token) -> list:
        params = {"origin": origin, "destination": destination, "depart_date": month,
                  "currency": "usd", "one_way": "true", "token": token}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self.URL_V1, params=params)
        except Exception as e:
            logger.error(f"Flights v1 error: {e}")
            return []
        if resp.status_code != 200: return []
        data = resp.json().get("data", {})
        flights = []
        for dest_key, transfers_dict in data.items():
            if not isinstance(transfers_dict, dict): continue
            for transfer_key, info in transfers_dict.items():
                if not isinstance(info, dict): continue
                try: transfers = int(transfer_key)
                except Exception: transfers = info.get("number_of_changes", 0)
                flights.append({
                    "airline": info.get("airline", ""), "price": info.get("price", 0),
                    "transfers": transfers, "departure_at": info.get("departure_at", ""),
                    "departure_hour": _departure_hour(info.get("departure_at", "")),
                    "duration": info.get("duration", 0),
                })
        return flights
