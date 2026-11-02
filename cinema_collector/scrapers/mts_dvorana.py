# cinema_collector/scrapers/mts_dvorana.py
# MTS Dvorana (mtsdvorana.rs). Сайт переписан на платформу TBeast (как и CineGrand),
# прежний разбор HTML (div.movie-item, ссылки ?date=) больше не работал (MTS_NO_DATES).
# Вся логика — в tbeast_v2.py.

from cinema_collector.scrapers.tbeast_v2 import TBeastV2Scraper

DEFAULT_API_URL = "https://api.mtsdvorana.rs/api/v2"
BASE_URL = "https://www.mtsdvorana.rs"


class MtsDvoranaScraper(TBeastV2Scraper):
    source = 'mts_dvorana'
    log_prefix = 'MTS'
    default_api_url = DEFAULT_API_URL
    default_site_url = BASE_URL
    default_location_id = 10         # «mts Dvorana» (id 8 «Web prodaja» — канал продаж, не зал)
