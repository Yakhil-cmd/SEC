Audit Report

## Title
Missing Response-Body Timeout in `send_post_request` Allows Indefinite Hang of Rosetta API Submission Path - (File: rs/rosetta-api/icp/src/ledger_client.rs)

## Summary
The `send_post_request` helper in `rs/rosetta-api/icp/src/ledger_client.rs` applies reqwest's `.timeout()` only to the `.send().await` phase (header receipt). The subsequent `resp.bytes().await` body-collection call carries no deadline. A Byzantine replica or boundary node that sends HTTP response headers promptly but then stalls on streaming the body will cause the Rosetta API async task to block indefinitely, permanently preventing that ICP ledger transaction submission from completing or timing out.

## Finding Description
In `rs/rosetta-api/icp/src/ledger_client.rs` at L838–859, `send_post_request` is:

```rust
async fn send_post_request(..., timeout: Duration) -> Result<...> {
    let resp = http_client
        .post(url)
        .timeout(timeout)   // covers only until headers are received
        .send()
        .await
        ...?;
    let resp_status = resp.status();
    let resp_body = resp
        .bytes()            // NO timeout; hangs indefinitely if body stalls
        .await
        ...?
        .to_vec();
    Ok((resp_body, resp_status))
}
```

In reqwest, `.timeout()` on a `RequestBuilder` wraps the `send()` future, which resolves when response headers are received. Once `send().await` returns a `Response`, the timeout wrapper is consumed. The `Response::bytes()` future is a separate, unguarded stream with no deadline.

`do_request` at L619–628 computes `wait_timeout = Self::TIMEOUT - start_time.elapsed()` and passes it to `send_post_request`, but this timeout only guards the header-receipt phase. The outer `while Instant::now() + poll_interval < deadline` loop at L619 is never re-evaluated while `send_post_request` is awaited, so the deadline check provides no protection against a stalled body read.

The correct pattern is demonstrated in `rs/canister_client/src/http_client.rs` at L215–234, where `tokio::time::timeout_at` is applied explicitly to both the response future and the body-collection future (`response.collect()`).

## Impact Explanation
This is a High-severity finding matching: *"Significant Rosetta, boundary/API, or infrastructure security impact with concrete user or protocol harm."* A single Byzantine replica or boundary node below the consensus fault threshold can cause any in-flight ICP ledger transaction submission routed through the affected Rosetta instance to hang permanently. The `do_request` call never returns, the transaction is never confirmed or rejected, and the Rosetta API's submission path for that task is permanently blocked. Exchanges and integrations relying on Rosetta for ICP transfers are directly impacted.

## Likelihood Explanation
A single Byzantine replica (below the consensus fault threshold) or a malicious boundary node can deliberately send HTTP `200 OK` headers and then withhold body bytes indefinitely. No governance majority, subnet majority, or special privilege is required — a single node acting below the fault threshold satisfies the Byzantine peer requirement. The Rosetta API's replica URL is configurable, and any node on the path that controls the TCP stream can trigger this. The condition is also reachable under realistic adverse network conditions (TCP window exhaustion, packet loss mid-stream) without any attacker involvement, making it both a security gap and a reliability hazard.

## Recommendation
Wrap `resp.bytes().await` in an explicit `tokio::time::timeout`, mirroring the pattern in `rs/canister_client/src/http_client.rs`:

```rust
let resp_body = tokio::time::timeout(timeout, resp.bytes())
    .await
    .map_err(|_| format!("receive post response timed out after {timeout:?}"))?
    .map_err(|err| format!("receive post response failed with {err}: "))?
    .to_vec();
```

This ensures the body-collection phase is bounded by the same `wait_timeout` already computed in `do_request`, closing the structural gap.

## Proof of Concept
1. Stand up a TCP server that immediately sends a valid HTTP/1.1 `200 OK\r\nContent-Length: 1000\r\n\r\n` response but then writes no body bytes and holds the connection open.
2. Configure the Rosetta API to use this server as its IC replica URL.
3. Submit any ICP ledger transaction via `/construction/submit`.
4. Observe that `send_post_request` never returns: the 20-second `TIMEOUT` deadline in `do_request` is never enforced for the body-read phase, and the Rosetta API worker is permanently blocked on `resp.bytes().await` with no timeout firing.