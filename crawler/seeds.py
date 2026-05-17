"""
crawler/seeds.py
----------------
Seed URLs for the DeepQuest historical archive crawler.

Contains ≥40 publicly accessible pages from historical archives that carry
dense narrative text about historical events, people, companies, and facts.
Grouped by source category to satisfy Req 1.1.

No logic — just the SEED_URLS list.
"""

SEED_URLS: list[str] = [

    # -------------------------------------------------------------------------
    # Internet Archive — text collections and historical document pages
    # -------------------------------------------------------------------------
    "https://archive.org/details/encyclopaediabri31chisrich",          # Encyclopaedia Britannica vol 31
    "https://archive.org/details/historyofuniteds00banc",              # History of the United States (Bancroft)
    "https://archive.org/details/annualreportofse1934unit",            # SEC Annual Report 1934
    "https://archive.org/details/historyofengland00macauoft",          # Macaulay's History of England
    "https://archive.org/details/worldalmanac1900newy",                # World Almanac 1900
    "https://archive.org/details/historyofamericanpeople01wils",       # History of the American People (Wilson)
    "https://archive.org/details/riseofamericanci00beeruoft",          # Rise of American Civilisation
    "https://archive.org/details/historyofwestern00wellsuoft",         # Outline of History (H.G. Wells)

    # -------------------------------------------------------------------------
    # Chronicling America — historical US newspaper archive pages
    # -------------------------------------------------------------------------
    "https://chroniclingamerica.loc.gov/lccn/sn83045462/1865-04-15/ed-1/seq-1/",   # NY Tribune, Lincoln assassination
    "https://chroniclingamerica.loc.gov/lccn/sn84026749/1906-04-19/ed-1/seq-1/",   # SF Call, 1906 earthquake
    "https://chroniclingamerica.loc.gov/lccn/sn83030214/1898-02-17/ed-1/seq-1/",   # NY World, USS Maine
    "https://chroniclingamerica.loc.gov/lccn/sn84026749/1929-10-30/ed-1/seq-1/",   # Black Tuesday, 1929 crash
    "https://chroniclingamerica.loc.gov/lccn/sn83045462/1917-04-07/ed-1/seq-1/",   # US enters WWI
    "https://chroniclingamerica.loc.gov/lccn/sn84026749/1919-01-17/ed-1/seq-1/",   # Prohibition ratification

    # -------------------------------------------------------------------------
    # HathiTrust Digital Library — historical books and documents
    # -------------------------------------------------------------------------
    "https://babel.hathitrust.org/cgi/pt?id=mdp.39015030510871",       # Annual Report of the Secretary of the Treasury 1890
    "https://babel.hathitrust.org/cgi/pt?id=uc1.b3625829",             # History of Standard Oil Company (Tarbell)
    "https://babel.hathitrust.org/cgi/pt?id=mdp.39015005549490",       # US Census 1880 compendium
    "https://babel.hathitrust.org/cgi/pt?id=uc2.ark:/13960/t4xg9p22g", # Carnegie Steel Company records
    "https://babel.hathitrust.org/cgi/pt?id=mdp.39015030510889",       # Annual Report of the Secretary of the Treasury 1900

    # -------------------------------------------------------------------------
    # SEC EDGAR — historical corporate filings
    # -------------------------------------------------------------------------
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=standard+oil&CIK=&type=10-K&dateb=&owner=include&count=40&search_text=",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=us+steel&CIK=&type=10-K&dateb=19700101&owner=include&count=40",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=general+motors&CIK=&type=10-K&dateb=19800101&owner=include&count=40",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=ford+motor&CIK=&type=10-K&dateb=19800101&owner=include&count=40",
    "https://efts.sec.gov/LATEST/search-index?q=%22merger%22+%22acquisition%22&dateRange=custom&startdt=1994-01-01&enddt=1999-12-31&forms=8-K",

    # -------------------------------------------------------------------------
    # US National Archives (archives.gov / catalog.archives.gov)
    # -------------------------------------------------------------------------
    "https://catalog.archives.gov/id/299998",                          # Declaration of Independence
    "https://catalog.archives.gov/id/1667751",                         # Treaty of Paris 1783
    "https://catalog.archives.gov/id/595398",                          # Emancipation Proclamation
    "https://catalog.archives.gov/id/7452250",                         # Manhattan Project records
    "https://www.archives.gov/research/military/world-war-2/overview", # WWII overview records
    "https://www.archives.gov/research/military/world-war-1/overview", # WWI overview records

    # -------------------------------------------------------------------------
    # Library of Congress (loc.gov)
    # -------------------------------------------------------------------------
    "https://www.loc.gov/collections/civil-war-maps/about-this-collection/",
    "https://www.loc.gov/collections/american-memory/about-this-collection/",
    "https://www.loc.gov/resource/gdcmassbookdig.historyofuniteds01ban/",  # Bancroft History vol 1
    "https://www.loc.gov/collections/railroad-maps-1828-to-1900/about-this-collection/",

    # -------------------------------------------------------------------------
    # UK National Archives (nationalarchives.gov.uk) — non-US portal #1
    # -------------------------------------------------------------------------
    "https://www.nationalarchives.gov.uk/education/resources/great-war-1914/",
    "https://www.nationalarchives.gov.uk/education/resources/industrial-revolution/",
    "https://discovery.nationalarchives.gov.uk/details/r/C14017",      # Cabinet papers WWI
    "https://www.nationalarchives.gov.uk/education/resources/empire-and-sea-power/",

    # -------------------------------------------------------------------------
    # Europeana — European cultural heritage collections — non-US portal #2
    # -------------------------------------------------------------------------
    "https://www.europeana.eu/en/collections/topic/48-world-war-i",
    "https://www.europeana.eu/en/collections/topic/83-industrial-revolution",
    "https://www.europeana.eu/en/collections/topic/190-newspapers",
    "https://www.europeana.eu/en/collections/topic/62-history",

    # -------------------------------------------------------------------------
    # Project Gutenberg — historical texts with dense narrative
    # -------------------------------------------------------------------------
    "https://www.gutenberg.org/ebooks/1232",    # The Prince — Machiavelli
    "https://www.gutenberg.org/ebooks/2600",    # War and Peace — Tolstoy
    "https://www.gutenberg.org/ebooks/5765",    # The Wealth of Nations — Adam Smith
    "https://www.gutenberg.org/ebooks/3207",    # Leviathan — Hobbes
    "https://www.gutenberg.org/ebooks/4943",    # The Federalist Papers

    # -------------------------------------------------------------------------
    # Wikipedia — historical lists with dense factual content
    # -------------------------------------------------------------------------
    "https://en.wikipedia.org/wiki/List_of_largest_corporate_mergers_and_acquisitions",
    "https://en.wikipedia.org/wiki/List_of_largest_companies_in_the_United_States_by_revenue",
    "https://en.wikipedia.org/wiki/List_of_presidents_of_the_United_States",
    "https://en.wikipedia.org/wiki/List_of_Nobel_laureates",
    "https://en.wikipedia.org/wiki/Timeline_of_United_States_history",
    "https://en.wikipedia.org/wiki/List_of_wars_involving_the_United_States",
    "https://en.wikipedia.org/wiki/List_of_largest_empires",
    "https://en.wikipedia.org/wiki/List_of_inventions_and_discoveries_of_the_Industrial_Revolution",
]
