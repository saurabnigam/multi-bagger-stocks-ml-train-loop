import yfinance as yf
import datetime
import re

# Simple heuristic sentiment analysis
POSITIVE_WORDS = ['growth', 'profit', 'up', 'surge', 'jump', 'rise', 'record', 'dividend', 'buy', 'strong', 'beat', 'beats', 'upgrade', 'improved', 'decline in npa']
NEGATIVE_WORDS = ['loss', 'down', 'fall', 'drop', 'slump', 'plunge', 'sell', 'weak', 'miss', 'misses', 'downgrade', 'penalty', 'fine', 'scam', 'fraud', 'slip']

def analyze_sentiment(ticker):
    """
    Fetches latest news for a ticker and runs keyword sentiment analysis.
    Returns: (sentiment_score, summary_string)
    """
    stock = yf.Ticker(ticker)
    news = stock.news
    
    if not news:
        return 0.0, "No recent news/concall data available."
        
    titles = []
    for item in news[:5]: # look at top 5 news items
        if 'content' in item and 'title' in item['content']:
            titles.append(item['content']['title'])
        elif 'title' in item:
            titles.append(item['title'])
            
    if not titles:
        return 0.0, "No recent news/concall data available."
        
    score = 0
    positive_count = 0
    negative_count = 0
    
    for t in titles:
        t_lower = t.lower()
        # Count words
        for w in POSITIVE_WORDS:
            if re.search(r'\b' + w + r'\b', t_lower):
                positive_count += 1
                score += 5
        for w in NEGATIVE_WORDS:
            if re.search(r'\b' + w + r'\b', t_lower):
                negative_count += 1
                score -= 5
                
    # Cap score
    score = max(-20, min(20, score))
    
    # Generate summary
    if score > 5:
        summary = f"Positive sentiment ({positive_count} bullish signals). Latest: '{titles[0]}'"
    elif score < -5:
        summary = f"Negative sentiment ({negative_count} bearish signals). Latest: '{titles[0]}'"
    else:
        summary = f"Neutral sentiment. Latest: '{titles[0]}'"
        
    return score, summary

if __name__ == '__main__':
    s, sum_text = analyze_sentiment('KOTAKBANK.NS')
    print(f"KOTAKBANK: Score={s}, Summary={sum_text}")
    s, sum_text = analyze_sentiment('TCS.NS')
    print(f"TCS: Score={s}, Summary={sum_text}")
