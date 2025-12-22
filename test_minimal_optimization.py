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
    print("🚀 Starting TRUE SEQUENTIAL Optimization POC")
    print("🔄 Each trial waits for ACTUAL completion of previous trial")
    print("⏱️  No artificial delays - pure request completion detection")
    print("🎯 Testing 3 BB stage parameters: normal_stage_bb, breakthrough_stage_bb, fallback_stage_bb")
    print()

    optimizer = MinimalOptimizer()

    try:
        optimizer.run_sequential_optimization(n_trials=10)
        print("\n✅ TRUE SEQUENTIAL optimization completed!")
        print("📁 Check 'minimal_optimization_results/' for detailed results")
    except KeyboardInterrupt:
        print("\n⚠️  Optimization interrupted by user")
    except Exception as e:
        print(f"\n❌ Optimization failed: {e}")
    finally:
        optimizer.close()

if __name__ == "__main__":
    main()