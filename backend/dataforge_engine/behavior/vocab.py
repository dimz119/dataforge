"""Static value pools for the faker-style generators (behavior-engine §7.3 /
plugin-architecture §4.1-§4.3).

These are deterministic lookup tables the generators index by a seeded draw — no
external faker dependency (BE-ENG-1: stdlib + jsonschema only). The pools are
small but realistic; a generator picks ``pool[u64 % len(pool)]``, so the same
draw always yields the same value (INV-GEN-3). Locale handling in v0 ships
``en_US`` only; an unknown locale is a validator concern (MAN-V402), not a
runtime one — the engine falls back to the default pool.
"""

from __future__ import annotations

FIRST_NAMES: tuple[str, ...] = (
    "Olivia", "Liam", "Emma", "Noah", "Ava", "Ethan", "Sophia", "Mason",
    "Isabella", "Lucas", "Mia", "Logan", "Amelia", "James", "Harper", "Aiden",
    "Evelyn", "Elijah", "Abigail", "Benjamin", "Rosa", "Diego", "Mei", "Omar",
    "Priya", "Kofi", "Yuki", "Ingrid", "Tariq", "Lucia",
)

LAST_NAMES: tuple[str, ...] = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Delgado", "Nguyen", "Patel", "Kim",
    "Okafor", "Tanaka", "Andersson", "Haddad", "Rossi", "Schmidt", "Lopez",
    "Wong", "Singh", "Hassan", "Ivanov", "Cohen", "Mwangi", "Costa", "Reyes",
    "Murphy",
)

STREET_NAMES: tuple[str, ...] = (
    "Maple", "Oak", "Cedar", "Pine", "Elm", "Washington", "Lake", "Hill",
    "Park", "Sunset", "River", "Highland", "Forest", "Meadow", "Spring",
    "Lincoln", "Jefferson", "Madison", "Birch", "Willow",
)
STREET_SUFFIX: tuple[str, ...] = ("St", "Ave", "Blvd", "Rd", "Ln", "Dr", "Way", "Ct")

CITIES: tuple[str, ...] = (
    "Springfield", "Riverside", "Franklin", "Greenville", "Bristol", "Clinton",
    "Fairview", "Salem", "Madison", "Georgetown", "Arlington", "Ashland",
    "Burlington", "Manchester", "Oxford", "Auburn", "Dover", "Newport",
)
STATES: tuple[str, ...] = (
    "CA", "TX", "NY", "FL", "IL", "PA", "OH", "GA", "NC", "MI", "WA", "AZ",
    "MA", "TN", "IN", "MO", "MD", "CO",
)
COUNTRIES: tuple[str, ...] = ("US", "CA", "GB", "DE", "FR", "AU", "JP", "BR")

PRODUCT_ADJ: tuple[str, ...] = (
    "Premium", "Classic", "Modern", "Rustic", "Sleek", "Compact", "Deluxe",
    "Eco", "Smart", "Vintage", "Pro", "Lite", "Ultra", "Essential",
)
PRODUCT_MATERIAL: tuple[str, ...] = (
    "Cotton", "Steel", "Bamboo", "Leather", "Ceramic", "Glass", "Oak",
    "Aluminum", "Wool", "Silicone", "Linen", "Copper",
)
PRODUCT_NOUN: tuple[str, ...] = (
    "Chair", "Lamp", "Mug", "Backpack", "Jacket", "Notebook", "Speaker",
    "Bottle", "Wallet", "Headphones", "Blanket", "Planter", "Knife", "Towel",
)
BRANDS: tuple[str, ...] = (
    "Acme", "Nordia", "Vertex", "Lumen", "Cascade", "Atlas", "Pioneer",
    "Meridian", "Summit", "Harbor", "Vela", "Onyx",
)
CATEGORIES: tuple[str, ...] = (
    "home", "electronics", "apparel", "outdoor", "kitchen", "office",
    "beauty", "sports", "toys", "garden",
)
CATEGORY_SUB: tuple[str, ...] = (
    "accessories", "essentials", "premium", "seasonal", "bestsellers", "new",
)

WORDS: tuple[str, ...] = (
    "lorem", "ipsum", "dolor", "amet", "consectetur", "adipiscing", "elit",
    "sed", "tempor", "incididunt", "labore", "magna", "aliqua", "veniam",
    "quis", "nostrud", "ullamco", "laboris", "aliquip", "commodo", "duis",
    "aute", "irure", "reprehenderit", "voluptate", "velit", "esse", "fugiat",
    "nulla", "pariatur",
)

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/120.0",
    "Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
)

EMAIL_DOMAINS: tuple[str, ...] = (
    "example.com", "mail.com", "inbox.net", "webmail.org", "post.io",
    "fastmail.co", "mailbox.dev", "letters.app", "send.email", "corp.example",
    "shop.example", "users.example",
)

URL_DOMAINS: tuple[str, ...] = (
    "example.com", "shop.example", "store.example", "market.example",
)
