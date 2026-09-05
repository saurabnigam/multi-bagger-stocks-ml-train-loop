"""
Headline keyword sentiment.

Despite the name, this does NOT read conference-call transcripts. It counts
positive/negative words in the last five Yahoo Finance news headlines for the
ticker, which are frequently about a different company (HEROMOTOCO's "latest
catalyst" on 2026-09-03 was an Ather Energy story). Its Rank IC across the
three logged periods was +0.118 / -0.007 / +0.040, so it is recorded for
diagnostics but no longer moves the final score (see quant_math.SENTIMENT_SCALE).
"""
import re
import yfinance as yf

POSITIVE_WORDS = ['growth', 'profit', 'up', 'surge', 'jump', 'rise', 'record', 'dividend', 'buy', 'strong', 'beat', 'beats', 'upgrade', 'improved', 'decline in npa']
NEGATIVE_WORDS = ['loss', 'down', 'fall', 'drop', 'slump', 'plunge', 'sell', 'weak', 'miss', 'misses', 'downgrade', 'penalty', 'fine', 'scam', 'fraud', 'slip']


def _titles(news):
    titles = []
    for item in (news or [])[:5]:
        if isinstance(item, dict):
            if 'content' in item and isinstance(item['content'], dict) and item['content'].get('title'):
                titles.append(item['content']['title'])
            elif item.get('title'):
                titles.append(item['title'])
    return titles


def score_titles(titles):
    score = 0
    positive_count = 0
    negative_count = 0
    for t in titles:
        t_lower = t.lower()
        for w in POSITIVE_WORDS:
            if re.search(r'\b' + w + r'\b', t_lower):
                positive_count += 1
                score += 5
        for w in NEGATIVE_WORDS:
            if re.search(r'\b' + w + r'\b', t_lower):
                negative_count += 1
                score -= 5
    return max(-20, min(20, score)), positive_count, negative_count


def analyze_sentiment(ticker, news=None):
    """
    Returns (sentiment_score in [-20, 20], summary_string).
    Pass `news` (the list from yf.Ticker(t).news) to avoid a second HTTP call.
    """
    if news is None:
        news = yf.Ticker(ticker).news

    titles = _titles(news)
    if not titles:
        return 0.0, "No recent news/concall data available."

    score, positive_count, negative_count = score_titles(titles)

    if score > 5:
        summary = f"Positive headline sentiment ({positive_count} bullish keywords). Latest: '{titles[0]}'"
    elif score < -5:
        summary = f"Negative headline sentiment ({negative_count} bearish keywords). Latest: '{titles[0]}'"
    else:
        summary = f"Neutral headline sentiment. Latest: '{titles[0]}'"

    return score, summary


if __name__ == '__main__':
    for t in ['KOTAKBANK.NS', 'TCS.NS']:
        s, sum_text = analyze_sentiment(t)
        print(f"{t}: Score={s}, Summary={sum_text}")
