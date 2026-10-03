"""Extracted from the legacy bot module without behavior changes."""

import logging

# ==================== ЛОГГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== КОНСТАНТЫ БЕЗОПАСНОСТИ ====================
MAX_FILE_SIZE = 10 * 1024 * 1024        # 10 MB максимум для одного файла
MAX_ZIP_UNCOMPRESSED = 100 * 1024 * 1024 # 100 MB максимум внутри ZIP
MAX_FILES_IN_ZIP = 500                   # Максимум файлов в архиве
MAX_LINES_IN_TXT = 10000                 # Максимум строк в TXT файле
MAX_ACCOUNTS_IN_TXT = 1000              # Максимум аккаунтов из TXT

# ============================================================
# ==================== STEAM ВАЛЮТЫ ====================
# ============================================================

STEAM_CURRENCIES = {
    1:  {'country': 'US', 'name': 'USD', 'symbol': '$', 'description': 'United States Dollar', 'min_commission': 2},      # $0.02
    2:  {'country': 'GB', 'name': 'GBP', 'symbol': '£', 'description': 'United Kingdom Pound', 'min_commission': 0.02},   # 0.02
    3:  {'country': 'EU', 'name': 'EUR', 'symbol': '€', 'description': 'European Union Euro', 'min_commission': 0.02},    # 0.02
    4:  {'country': 'CH', 'name': 'CHF', 'symbol': 'CHF', 'description': 'Swiss Francs', 'min_commission': None},         # неизвестно
    5:  {'country': 'RU', 'name': 'RUB', 'symbol': '₽', 'description': 'Russian Rouble', 'min_commission': 158},          # 1.58₽
    6:  {'country': 'PL', 'name': 'PLN', 'symbol': 'zł', 'description': 'Polish Złoty', 'min_commission': None},          # неизвестно
    7:  {'country': 'BR', 'name': 'BRL', 'symbol': 'R$', 'description': 'Brazilian Reals', 'min_commission': None},       # неизвестно
    8:  {'country': 'JP', 'name': 'JPY', 'symbol': '¥', 'description': 'Japanese Yen', 'min_commission': None},           # неизвестно
    9:  {'country': 'NO', 'name': 'NOK', 'symbol': 'kr', 'description': 'Norwegian Krone', 'min_commission': None},        # неизвестно
    10: {'country': 'ID', 'name': 'IDR', 'symbol': 'Rp', 'description': 'Indonesian Rupiah', 'min_commission': None},      # неизвестно
    11: {'country': 'MY', 'name': 'MYR', 'symbol': 'RM', 'description': 'Malaysian Ringgit', 'min_commission': None},     # неизвестно
    12: {'country': 'PH', 'name': 'PHP', 'symbol': '₱', 'description': 'Philippine Peso', 'min_commission': None},        # неизвестно
    13: {'country': 'SG', 'name': 'SGD', 'symbol': 'S$', 'description': 'Singapore Dollar', 'min_commission': None},       # неизвестно
    14: {'country': 'TH', 'name': 'THB', 'symbol': '฿', 'description': 'Thai Baht', 'min_commission': None},              # неизвестно
    15: {'country': 'VN', 'name': 'VND', 'symbol': '₫', 'description': 'Vietnamese Dong', 'min_commission': None},        # неизвестно
    16: {'country': 'KR', 'name': 'KRW', 'symbol': '₩', 'description': 'South Korean Won', 'min_commission': None},       # неизвестно
    18: {'country': 'UA', 'name': 'UAH', 'symbol': '₴', 'description': 'Ukrainian Hryvnia', 'min_commission': 200},       # 2₴
    19: {'country': 'MX', 'name': 'MXN', 'symbol': 'Mex$', 'description': 'Mexican Peso', 'min_commission': None},        # неизвестно
    20: {'country': 'CA', 'name': 'CAD', 'symbol': 'CDN$', 'description': 'Canadian Dollars', 'min_commission': None},    # неизвестно
    21: {'country': 'AU', 'name': 'AUD', 'symbol': 'A$', 'description': 'Australian Dollars', 'min_commission': None},    # неизвестно
    22: {'country': 'NZ', 'name': 'NZD', 'symbol': 'NZ$', 'description': 'New Zealand Dollar', 'min_commission': None},   # неизвестно
    23: {'country': 'CN', 'name': 'CNY', 'symbol': '¥', 'description': 'Chinese Renminbi (yuan)', 'min_commission': None},# неизвестно
    24: {'country': 'IN', 'name': 'INR', 'symbol': '₹', 'description': 'Indian Rupee', 'min_commission': None},           # неизвестно
    25: {'country': 'CL', 'name': 'CLP', 'symbol': 'CLP$', 'description': 'Chilean Peso', 'min_commission': None},        # неизвестно
    26: {'country': 'PE', 'name': 'PEN', 'symbol': 'S/', 'description': 'Peruvian Sol', 'min_commission': None},          # неизвестно
    27: {'country': 'CO', 'name': 'COP', 'symbol': 'COL$', 'description': 'Colombian Peso', 'min_commission': None},      # неизвестно
    28: {'country': 'ZA', 'name': 'ZAR', 'symbol': 'R', 'description': 'South African Rand', 'min_commission': None},     # неизвестно
    29: {'country': 'HK', 'name': 'HKD', 'symbol': 'HK$', 'description': 'Hong Kong Dollar', 'min_commission': None},     # неизвестно
    30: {'country': 'TW', 'name': 'TWD', 'symbol': 'NT$', 'description': 'New Taiwan Dollar', 'min_commission': None},    # неизвестно
    31: {'country': 'SA', 'name': 'SAR', 'symbol': 'SR', 'description': 'Saudi Riyal', 'min_commission': None},           # неизвестно
    32: {'country': 'AE', 'name': 'AED', 'symbol': 'AED', 'description': 'United Arab Emirates Dirham', 'min_commission': None}, # неизвестно
    34: {'country': 'AR', 'name': 'ARS', 'symbol': 'ARS$', 'description': 'Argentine Peso', 'min_commission': None},      # неизвестно
    35: {'country': 'IL', 'name': 'ILS', 'symbol': '₪', 'description': 'Israeli New Shekel', 'min_commission': None},     # неизвестно
    37: {'country': 'KZ', 'name': 'KZT', 'symbol': '₸', 'description': 'Kazakhstani Tenge', 'min_commission': None},      # неизвестно
    38: {'country': 'KW', 'name': 'KWD', 'symbol': 'KD', 'description': 'Kuwaiti Dinar', 'min_commission': None},         # неизвестно
    39: {'country': 'QA', 'name': 'QAR', 'symbol': 'QR', 'description': 'Qatari Riyal', 'min_commission': None},          # неизвестно
    40: {'country': 'CR', 'name': 'CRC', 'symbol': '₡', 'description': 'Costa Rican Colón', 'min_commission': None},      # неизвестно
    41: {'country': 'UY', 'name': 'UYU', 'symbol': '$U', 'description': 'Uruguayan Peso', 'min_commission': None},        # неизвестно
}
