"""secdata — fetch U.S. stock data from SEC EDGAR, one ticker at a time.

Ten pipelines, each importable as a module and callable as a subcommand:

    secdata filings    bulk filing index + documents, filtered by form      (1)
    secdata reports    10-K / 10-Q / 20-F archive + incremental refresh     (2)
    secdata xbrl       Company Facts / XBRL financial ingestion             (3)
    secdata prexbrl    pre-2009 filings download + text extraction          (4)
    secdata holdings   13F & N-PORT holdings, parsers + comparison          (5)
    secdata insider    Form 4 insider-transaction fetch + parse             (6)
    secdata events     8-K download + item-code event indexing              (7)
    secdata proxy      DEF 14A extraction + executive intelligence          (8)
    secdata preipo     S-1 / registration statement monitor                 (9)
    secdata enforcement SEC enforcement releases scraper                   (10)
"""
__version__ = "0.1.0"
