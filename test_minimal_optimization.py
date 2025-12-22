#!/usr/bin/env python3
"""
Test runner for minimal optimization POC
"""

import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from scripts.minimal_optimization_poc import MinimalOptimizer

def main():
    print("🚀 Starting Minimal Optimization POC")
    print("📊 This will run 3 trials with 2-second delays between requests")
    print("⏱️  Expected time: ~6-10 minutes total")
    print()

    optimizer = MinimalOptimizer()

    try:
        optimizer.run_optimization(n_trials=3)
        print("\n✅ Optimization completed! Check 'minimal_optimization_results/' for results")
    except KeyboardInterrupt:
        print("\n⚠️  Optimization interrupted by user")
    except Exception as e:
        print(f"\n❌ Optimization failed: {e}")
    finally:
        optimizer.close()

if __name__ == "__main__":
    main()