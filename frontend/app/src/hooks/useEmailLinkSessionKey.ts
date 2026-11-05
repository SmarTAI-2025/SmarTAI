import { useLayoutEffect, useState } from "react";
import { useLocation } from "react-router-dom";

/** A new mail fragment starts a fresh in-memory flow; removing it does not. */
export function useEmailLinkSessionKey(): number {
  const { hash, key } = useLocation();
  const [generation, setGeneration] = useState(0);
  useLayoutEffect(() => {
    if (hash) setGeneration((value) => value + 1);
  }, [hash, key]);
  return generation;
}
