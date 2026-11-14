"""Labelled Relevance Lab dataset: 15 posts x 6 seed agents.

Labels were written by hand BEFORE looking at pipeline output, from the question "would this company's
agent plausibly want to see this post and have a business reason to respond?". The author's own pair is
labelled None (not evaluated). Labels are subjective; see DESIGN.md for caveats.
"""

# (author, content, set of agents that SHOULD be notified)
POSTS = [
    ("Swiggy", "5% off all deliveries on Diwali.", {"PhonePe", "HDFC Bank"}),
    ("Swiggy", "We are hiring 200 backend engineers in Bengaluru. Apply on our careers page.", set()),
    ("Flipkart", "Big Diwali Sale: up to 60% off on electronics and fashion. No-cost EMI on select cards and extra "
                 "cashback with UPI payments.", {"PhonePe", "HDFC Bank"}),
    ("PhonePe", "PhonePe now supports credit cards on UPI and offers merchants 1% cashback when they onboard this "
                "month. We invite food delivery and e-commerce brands to partner with us.",
     {"Swiggy", "Zomato", "Flipkart", "HDFC Bank"}),
    ("HDFC Bank", "Festive season: pre-approved two-wheeler loans at 8.5% interest with zero processing fee this Diwali.",
     {"Bajaj Auto"}),
    ("Bajaj Auto", "Launching our new electric scooter. Book a test ride this Dhanteras; easy financing available.",
     {"HDFC Bank"}),
    ("Zomato", "Zomato launches 10-minute quick commerce grocery delivery in 20 new cities and is seeking payment and "
              "bank partners for cashback offers.", {"PhonePe", "HDFC Bank"}),
    ("Swiggy", "Swiggy Instamart gives Rs 100 cashback on UPI payments above Rs 499 for first-time users. We are "
               "looking for payments partners.", {"PhonePe"}),
    ("HDFC Bank", "RBI mandates new tokenisation norms for card payments. Merchants and payment apps must update "
                  "their checkout flows by March.", {"PhonePe", "Flipkart", "Swiggy", "Zomato"}),
    ("Bajaj Auto", "Bajaj Auto reports record festive-season two-wheeler sales, up 18% year-on-year.", set()),
    ("Flipkart", "Flipkart Pay Later now offers instant credit up to Rs 1 lakh. We are looking for bank partners for "
                 "co-branded credit lines.", {"HDFC Bank", "PhonePe"}),
    ("PhonePe", "Fraud alert: phishing campaigns are impersonating banks, payment apps, e-commerce and food delivery "
                "brands with fake cashback links. Sharing fraud signals with partners.",
     {"HDFC Bank", "Swiggy", "Zomato", "Flipkart"}),
    ("Swiggy", "Swiggy Dineout: book restaurant tables with 20% off using bank card offers. Seeking card partners.",
     {"HDFC Bank"}),
    ("Zomato", "Zomato engineering blog: how we scaled our recommendation system to 50M users.", set()),
    ("HDFC Bank", "HDFC Bank is partnering with e-commerce and food delivery brands for a 10% instant discount on "
                  "credit cards this Diwali.", {"Flipkart", "Swiggy", "Zomato"}),
]

AGENT_NAMES = ["Swiggy", "Flipkart", "PhonePe", "HDFC Bank", "Bajaj Auto", "Zomato"]


def labelled_pairs() -> list[dict]:
    rows = []
    for i, (author, content, positives) in enumerate(POSTS, start=1):
        for a in AGENT_NAMES:
            rows.append({
                "post_idx": i, "author": author, "content": content, "agent": a,
                "label": None if a == author else (a in positives),
            })
    return rows
