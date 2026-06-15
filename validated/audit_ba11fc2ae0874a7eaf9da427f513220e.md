### Title
Predictable `PREVRANDAO` via `keccak256(block_timestamp)` Enables Randomness Manipulation in All EVM Contracts - (`x/evm/keeper/keeper.go`, `giga/deps/xevm/keeper/keeper.go`)

---

### Summary

Sei's EVM implementation derives the `PREVRANDAO` opcode value by hashing the block timestamp (`keccak256(block_timestamp)`). Because Sei uses Proposer-Based Timestamps (PBTS), the block timestamp is set by the current round's proposer from its local clock and is bounded by a narrow, publicly known synchrony window. This makes `PREVRANDAO` predictable by any observer and directly manipulable by the block proposer — without requiring any leaked keys or privileged infrastructure. Any EVM contract on Sei that uses `block.prevrandao` as a source of randomness (NFT launches, lotteries, on-chain games) is vulnerable to exploitation.

---

### Finding Description

In `GetVMBlockContext`, both the main EVM keeper and the giga EVM keeper compute `PREVRANDAO` as:

```go
// Use hash of block timestamp as info for PREVRANDAO
r, err := ctx.BlockHeader().Time.MarshalBinary()
rh := crypto.Keccak256Hash(r)
...
Random: &rh,
``` [1](#0-0) [2](#0-1) 

The `Random` field in `vm.BlockContext` is what the EVM exposes as `block.prevrandao` (opcode `PREVRANDAO`, formerly `DIFFICULTY`) to Solidity contracts. On Ethereum post-Merge, this is sourced from the beacon chain's RANDAO, which is cryptographically unpredictable. On Sei, it is simply `keccak256(block_timestamp)`.

Under Sei's PBTS consensus, the block timestamp is assigned by the round proposer from its local clock and must satisfy:

```
localtime >= proposedBlockTime - Precision
localtime <= proposedBlockTime + MsgDelay + Precision
``` [3](#0-2) 

This means the timestamp is:
1. **Known to the proposer** before the block is finalized and before any transactions in that block are executed.
2. **Approximately predictable by any observer** — given Sei's ~400ms block time, the next block's timestamp is predictable within the `PRECISION + MSGDELAY` window (typically a few hundred milliseconds), which maps to a small, enumerable set of possible `PREVRANDAO` values.
3. **Directly manipulable by the proposer** — the proposer can choose any timestamp within the allowed window to produce a desired `keccak256` output.

---

### Impact Explanation

Any EVM contract deployed on Sei that uses `block.prevrandao` as a source of randomness is broken at the chain level. This includes:

- **NFT launches** using `block.prevrandao` to assign token IDs or reveal metadata (the exact vulnerability class from the external report).
- **On-chain lotteries and gambling contracts** using `block.prevrandao` to determine winners.
- **Any commit-reveal or randomness-dependent protocol** relying on `PREVRANDAO`.

The block proposer (a validator) can enumerate all valid timestamps in the allowed window, compute `keccak256(t)` for each, and choose the timestamp that produces the most favorable `PREVRANDAO` for their own transactions. An unprivileged attacker who is not a validator can still predict the `PREVRANDAO` value with high confidence by observing the chain's block timing pattern and computing `keccak256(expected_timestamp)` before submitting their transaction.

This is a **chain-level** vulnerability — it is not caused by any individual contract's design, but by Sei's implementation of the `PREVRANDAO` opcode itself.

---

### Likelihood Explanation

- **Unprivileged attacker**: Can predict `PREVRANDAO` with high probability by observing the chain's block cadence. Sei's ~400ms block time means the timestamp search space is small. No special access is required.
- **Validator/proposer**: Can guarantee a specific `PREVRANDAO` value by selecting the timestamp within the PBTS-allowed window. This requires being the current round's proposer, which rotates among validators.
- **Exploitability**: Any contract using `block.prevrandao` for randomness is immediately exploitable. The attack requires only a standard EVM transaction.

---

### Recommendation

Replace the timestamp-based `PREVRANDAO` derivation with a value that is not predictable before block finalization. Options include:

1. **Use the previous block's hash** (already available via `GetHashFn`) mixed with additional entropy (e.g., `keccak256(prevBlockHash || blockHeight)`). This is not perfect but significantly harder to predict.
2. **Integrate a VRF** (Verifiable Random Function) at the consensus layer, where validators contribute to a shared random beacon per block.
3. **Use the block's `AppHash`** (computed after all transactions are applied) as the `PREVRANDAO` for the *next* block, similar to how Ethereum's RANDAO accumulates entropy across blocks.

At minimum, document clearly that `block.prevrandao` on Sei is not a secure source of randomness, so contract developers are not misled by Ethereum compatibility assumptions.

---

### Proof of Concept

1. Deploy a simple lottery contract on Sei that uses `block.prevrandao % N` to pick a winner.
2. Before submitting the winning transaction, observe the latest block timestamp `T`.
3. Compute `keccak256(T + ~400ms)` (the expected next block timestamp) to predict `PREVRANDAO`.
4. If the predicted value is favorable, submit the transaction; otherwise wait one block and repeat.
5. A validator acting as proposer can directly set the timestamp to the value that produces the desired `PREVRANDAO`, guaranteeing the outcome.

The `EVMCompatibilityTester.sol` in the repository already demonstrates that `block.prevrandao` is accessible and used in contracts:

```solidity
uint256 randomNumber = uint256(keccak256(abi.encodePacked(block.number, block.prevrandao, counter)));
``` [4](#0-3) 

Since `block.prevrandao` = `keccak256(block_timestamp)` on Sei, this "random number" is fully predictable before the block is finalized.

### Citations

**File:** x/evm/keeper/keeper.go (L267-272)
```go
	// Use hash of block timestamp as info for PREVRANDAO
	r, err := ctx.BlockHeader().Time.MarshalBinary()
	if err != nil {
		return nil, err
	}
	rh := crypto.Keccak256Hash(r)
```

**File:** giga/deps/xevm/keeper/keeper.go (L260-265)
```go
	// Use hash of block timestamp as info for PREVRANDAO
	r, err := ctx.BlockHeader().Time.MarshalBinary()
	if err != nil {
		return nil, err
	}
	rh := crypto.Keccak256Hash(r)
```

**File:** sei-tendermint/types/proposal.go (L96-123)
```go
func (p *Proposal) IsTimely(recvTime time.Time, sp SynchronyParams, round int32) bool {
	// The message delay values are scaled as rounds progress.
	// Every 10 rounds, the message delay is doubled to allow consensus to
	// proceed in the case that the chosen value was too small for the given network conditions.
	// For more information and discussion on this mechanism, see the relevant github issue:
	// https://github.com/tendermint/spec/issues/371
	if round < 0 {
		return false
	}
	maxShift := bits.LeadingZeros64(uint64(sp.MessageDelay)) - 1 //nolint:gosec // message delay is non zero
	nShift := int(round / 10)                                    //nolint:gosec // round is validated non-negative above

	if nShift > maxShift {
		// if the number of 'doublings' would overflow the size of the int, use the
		// maximum instead.
		nShift = maxShift
	}
	msgDelay := sp.MessageDelay * time.Duration(1<<nShift)

	// lhs is `proposedBlockTime - Precision` in the first inequality
	lhs := p.Timestamp.Add(-sp.Precision)
	// rhs is `proposedBlockTime + MsgDelay + Precision` in the second inequality
	rhs := p.Timestamp.Add(msgDelay).Add(sp.Precision)

	if recvTime.Before(lhs) || recvTime.After(rhs) {
		return false
	}
	return true
```

**File:** contracts/src/EVMCompatibilityTester.sol (L205-205)
```text
            uint256 randomNumber = uint256(keccak256(abi.encodePacked(block.number, block.prevrandao, counter)));
```
