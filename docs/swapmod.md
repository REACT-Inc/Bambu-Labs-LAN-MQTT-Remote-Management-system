# Swapmod

Swapmod swaps the build plate by **running a swap print**: a sliced `.3mf` whose G-code swaps the plate (for example from the Swapmod kit). The app runs it between your queued prints, so the queue keeps going without anyone clearing the plate.

Offered for the A1 family (A1 / A1 mini), the printers Swapmod kits fit.

## Set it up

In the dashboard, open the printer → **More → Swapmod**:

1. **Swap print file:** choose your swap `.3mf`. If it has several sliced plates, pick the one to use.
2. **Turn it on:** tick **Swap the plate automatically after each finished print, then start the next queued job**.

That's all. The app doesn't count plates or approve batches.

## What happens

1. You start a job from the queue as usual.
2. When the print **finishes**, the app starts the swap print. It's a normal queue job, uploaded and started the same way as any other.
3. When the swap print finishes, the **next waiting job** for that printer starts on its own. If nothing is waiting, you're told the fresh plate is ready.
4. After a **failed** print it doesn't swap on its own, because the cause (a clog, a run-out) would just repeat. You're told, and **Swap plate now** runs the swap whenever you want.
5. If the swap print itself fails, the queue stops and you're told.

**Swap plate now** (dashboard) or `/plateswap now` runs the swap print straight away.

## Discord

```text
/plateswap set name:Steward enabled:true
/plateswap now name:Steward
```

The swap print file is chosen in the dashboard.

## Things to know

- **Load enough plates.** The app doesn't know how many are left. If the kit runs out, the swap print fails and the queue stops.
- **The swap print must be sliced for this printer model**, like any other print.
- **Starting the next job is automatic** while Swapmod is on: the printer must still be ready and error-free, as for any start.
