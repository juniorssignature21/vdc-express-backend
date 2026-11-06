"""Countries and rate zones, modelled on how express integrators (DHL Express, UPS, FedEx) zone their rate cards.

Zones are measured from the home market (settings.TRACKPRO["DOMESTIC_COUNTRY"], the US by default):
  1 Canada & Mexico · 2 Western Europe · 3 Rest of Europe · 4 Caribbean, Central & South America
  5 Major Asia-Pacific · 6 Rest of Asia & Middle East · 7 Africa · 8 Pacific islands & remote
"""

_ZONES = {
    1: "CA:Canada|MX:Mexico",
    2: "AD:Andorra|AT:Austria|BE:Belgium|CH:Switzerland|DE:Germany|DK:Denmark|ES:Spain|FI:Finland|FR:France|"
       "GB:United Kingdom|IE:Ireland|IS:Iceland|IT:Italy|LI:Liechtenstein|LU:Luxembourg|MC:Monaco|MT:Malta|"
       "NL:Netherlands|NO:Norway|PT:Portugal|SE:Sweden|SM:San Marino|VA:Vatican City",
    3: "AL:Albania|AM:Armenia|AZ:Azerbaijan|BA:Bosnia and Herzegovina|BG:Bulgaria|CY:Cyprus|CZ:Czechia|EE:Estonia|"
       "GE:Georgia|GR:Greece|HR:Croatia|HU:Hungary|LT:Lithuania|LV:Latvia|MD:Moldova|ME:Montenegro|"
       "MK:North Macedonia|PL:Poland|RO:Romania|RS:Serbia|SI:Slovenia|SK:Slovakia|TR:Türkiye|UA:Ukraine",
    4: "AG:Antigua and Barbuda|AR:Argentina|AW:Aruba|BB:Barbados|BO:Bolivia|BR:Brazil|BS:Bahamas|BZ:Belize|"
       "CL:Chile|CO:Colombia|CR:Costa Rica|CW:Curaçao|DM:Dominica|DO:Dominican Republic|EC:Ecuador|"
       "GD:Grenada|GT:Guatemala|GY:Guyana|HN:Honduras|HT:Haiti|JM:Jamaica|KN:Saint Kitts and Nevis|"
       "KY:Cayman Islands|LC:Saint Lucia|NI:Nicaragua|PA:Panama|PE:Peru|PR:Puerto Rico|PY:Paraguay|"
       "SR:Suriname|SV:El Salvador|TC:Turks and Caicos Islands|TT:Trinidad and Tobago|UY:Uruguay|"
       "VC:Saint Vincent and the Grenadines|VE:Venezuela|VG:British Virgin Islands|VI:U.S. Virgin Islands|BM:Bermuda",
    5: "AU:Australia|CN:China|HK:Hong Kong|JP:Japan|KR:South Korea|MO:Macau|NZ:New Zealand|SG:Singapore|TW:Taiwan",
    6: "AE:United Arab Emirates|AF:Afghanistan|BD:Bangladesh|BH:Bahrain|BN:Brunei|BT:Bhutan|ID:Indonesia|"
       "IL:Israel|IN:India|IQ:Iraq|JO:Jordan|KG:Kyrgyzstan|KH:Cambodia|KW:Kuwait|KZ:Kazakhstan|LA:Laos|"
       "LB:Lebanon|LK:Sri Lanka|MM:Myanmar|MN:Mongolia|MV:Maldives|MY:Malaysia|NP:Nepal|OM:Oman|PH:Philippines|"
       "PK:Pakistan|QA:Qatar|SA:Saudi Arabia|TH:Thailand|TJ:Tajikistan|TM:Turkmenistan|UZ:Uzbekistan|"
       "VN:Vietnam|YE:Yemen",
    7: "AO:Angola|BF:Burkina Faso|BI:Burundi|BJ:Benin|BW:Botswana|CD:DR Congo|CF:Central African Republic|"
       "CG:Congo|CI:Côte d'Ivoire|CM:Cameroon|CV:Cape Verde|DJ:Djibouti|DZ:Algeria|EG:Egypt|ER:Eritrea|"
       "ET:Ethiopia|GA:Gabon|GH:Ghana|GM:Gambia|GN:Guinea|GQ:Equatorial Guinea|GW:Guinea-Bissau|KE:Kenya|"
       "KM:Comoros|LR:Liberia|LS:Lesotho|LY:Libya|MA:Morocco|MG:Madagascar|ML:Mali|MR:Mauritania|MU:Mauritius|"
       "MW:Malawi|MZ:Mozambique|NA:Namibia|NE:Niger|NG:Nigeria|RW:Rwanda|SC:Seychelles|SD:Sudan|SL:Sierra Leone|"
       "SN:Senegal|SO:Somalia|SS:South Sudan|ST:São Tomé and Príncipe|SZ:Eswatini|TD:Chad|TG:Togo|TN:Tunisia|"
       "TZ:Tanzania|UG:Uganda|ZA:South Africa|ZM:Zambia|ZW:Zimbabwe",
    8: "CK:Cook Islands|FJ:Fiji|FM:Micronesia|GU:Guam|KI:Kiribati|MH:Marshall Islands|NC:New Caledonia|NR:Nauru|"
       "PF:French Polynesia|PG:Papua New Guinea|PW:Palau|SB:Solomon Islands|TO:Tonga|TV:Tuvalu|VU:Vanuatu|WS:Samoa",
}

COUNTRIES: dict[str, dict] = {"US": {"name": "United States", "zone": 0}}
for _zone, _rows in _ZONES.items():
    for _row in _rows.split("|"):
        _code, _name = _row.split(":", 1)
        COUNTRIES[_code] = {"name": _name, "zone": _zone}

# Embargoed / suspended – no online booking (DHL Express and the other US integrators don't serve these).
NOT_SERVED = {"CU": "Cuba", "IR": "Iran", "KP": "North Korea", "SY": "Syria", "RU": "Russia", "BY": "Belarus"}

# Elevated-risk destinations carry a per-shipment security surcharge.
ELEVATED_RISK = {"AF", "BF", "CD", "CF", "HT", "IQ", "LY", "ML", "NE", "SD", "SO", "SS", "UA", "VE", "YE"}

ZONE_NAMES = {
    0: "Domestic", 1: "Canada & Mexico", 2: "Western Europe", 3: "Rest of Europe",
    4: "Caribbean, Central & South America", 5: "Major Asia-Pacific", 6: "Rest of Asia & Middle East",
    7: "Africa", 8: "Pacific islands & remote",
}


def country_name(code: str) -> str:
    code = (code or "").upper()
    return COUNTRIES.get(code, {}).get("name") or NOT_SERVED.get(code) or code


def country_list() -> list[dict]:
    return sorted(({"code": c, "name": v["name"]} for c, v in COUNTRIES.items()), key=lambda c: c["name"])
