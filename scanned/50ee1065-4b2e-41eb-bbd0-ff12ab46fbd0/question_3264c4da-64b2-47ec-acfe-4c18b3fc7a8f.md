[File: 'etc/eth-contracts/contracts/IExit.sol -> Scope: Critical. Insolvency'] [Function: EvmErc20.withdrawToNear / ExitToNear.run (ERC-20 Omni path, ft_transfer_call partial refund)] Can an unprivileged EVM user call withdrawToNear with an Omni message (colon-separated msg) under the precondition that the target NEP-141 ft_transfer_call partially refunds tokens (returns a non-zero refund amount less than the full amount), causing the sequence _burn(sender, amount) -> ExitToNear schedules ft_transfer_call(amount, msg) -> callback receives Successful result with partial refund -> refund_call_args uses the original event amount

```python
questions = [
