# cinema_collector/scrapers/cinegrand.py
# CineGrand (cinegrand-mcf.rs). С ноября 2026 сайт переписан, API другое (v2, открытое).
# Прежний адрес stagetest.gart.rs:9393 (TBeastAPI) удалён из DNS. Вся логика — в tbeast_v2.py.

from cinema_collector.scrapers.tbeast_v2 import TBeastV2Scraper

DEFAULT_API_URL = "https://api.cinegrand-mcf.rs/api/v2"
BASE_URL = "https://cinegrand-mcf.rs"


class CineGrandScraper(TBeastV2Scraper):
    source = 'cinegrand'
    log_prefix = 'CINEGRAND'
    default_api_url = DEFAULT_API_URL
    default_site_url = BASE_URL
    default_location_id = 1          # Cine Grand Delta Planet (список: GET {api_url}/locations)
