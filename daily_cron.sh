#!/bin/bash

# V16 AI Quant Engine Automated Pipeline
echo "Starting V16 Pipeline: $(date)"

cd /Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch
source venv/bin/activate

# 1. Run the Self-Learning Optimizer (Adjusts DB Weights based on Performance)
python weight_optimizer.py

# 2. Scrape Nifty 50 & Score using optimized weights (Saves to DB)
python harness_v16_learning.py

# 3. Update the UI Dashboard JSON from the DB
python update_ui_v16.py

echo "V16 Pipeline Complete: $(date)"
