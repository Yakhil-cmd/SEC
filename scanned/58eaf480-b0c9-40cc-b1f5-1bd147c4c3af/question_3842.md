# Q3842: Get Signers ibc invariant edge fa5e

## Question
Can an unprivileged attacker reach `GetSigners` in `sei-ibc-go/modules/core/03-connection/types/msgs.go` via IBC packet, acknowledgement, timeout, or proof relay submitted through public message handlers, controlling packet data, source/destination channel fields, timeout height/timestamp, acknowledgements, proofs, memo fields, and relayer timing, and cause escrow, voucher supply, or channel accounting to diverge through timeout, acknowledgement, or replay ordering so that the invariant `escrowed funds, voucher supply, denom traces, acknowledgements, and timeout state must remain conserved across all packet flows` fails, causing `High: Permanent chain split requiring hard fork`?

## Target
- File/function: `sei-ibc-go/modules/core/03-connection/types/msgs.go:241` `GetSigners`
- Entrypoint: IBC packet, acknowledgement, timeout, or proof relay submitted through public message handlers
- Attacker controls: packet data, source/destination channel fields, timeout height/timestamp, acknowledgements, proofs, memo fields, and relayer timing
- Exploit idea: cause escrow, voucher supply, or channel accounting to diverge through timeout, acknowledgement, or replay ordering
- Invariant to test: escrowed funds, voucher supply, denom traces, acknowledgements, and timeout state must remain conserved across all packet flows
- Expected Immunefi impact: High: Permanent chain split requiring hard fork
- Fast validation: Use IBC keeper/channel tests to relay packet, ack, timeout, and replay variants, then assert escrow, voucher supply, commitments, and acknowledgements remain conserved.
