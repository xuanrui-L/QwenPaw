import { useEffect, useState } from "react";

/** Keep unsaved design edits during polling, and expose concurrent changes. */
export function useDesignDraft(initial: string) {
  const [state, setState] = useState({ base: initial, value: initial });
  useEffect(() => {
    setState((current) =>
      current.base === current.value && current.base !== initial
        ? { base: initial, value: initial }
        : current,
    );
  }, [initial]);
  return {
    value: state.value,
    base: state.base,
    dirty: state.value !== state.base,
    conflict: state.base !== initial && state.value !== state.base,
    change: (value: string) => setState((current) => ({ ...current, value })),
    reset: () => setState({ base: initial, value: initial }),
    saved: () =>
      setState((current) => ({ base: current.value, value: current.value })),
  };
}
