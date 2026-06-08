#!/usr/bin/env python3
import sys
from db import watchlist_col

def check_watchlist():
    if watchlist_col is None:
        print("0")
        return
    
    count = watchlist_col.count_documents({"status": "ACTIVE"})
    print(count)

if __name__ == "__main__":
    check_watchlist()
