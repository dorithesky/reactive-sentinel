# Vendored dependency

`reactive-lib-omni` is vendored (not a git submodule) so the repository builds offline and a judge can read the
exact interfaces the contracts compile against.

* Source: <https://github.com/Reactive-Network/reactive-lib-omni>
* Commit: `3ade0dcf1d27e67e5783ef807e73a1b9e0c0cce8` (2026-05-21, "Initial implementation.")
* Remapping: `@reactive/=lib/reactive-lib-omni/`

Files used:

* `src/interfaces/ISystemContract.sol` — `subscribe`, `unsubscribe`, `requestCallbackV_1_0`,
  `CallbackConfiguration_V_1_0`
* `src/interfaces/IReactive.sol` — `LogRecord`, `react`
* `src/base/AbstractReactive.sol` — `SYSTEM`, `REACTIVE_IGNORE`, `onlySystem`
* `src/base/AbstractCallback.sol` — `_CALLBACK_SENDER`, `onlyCallbackSender`
* `src/base/AbstractPayer.sol` — `pay`, `_coverDebt`, `receive`

Do not edit these files; re-vendor from upstream when the library changes.
