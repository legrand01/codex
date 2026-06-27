import { useState, useEffect, useCallback } from 'react';
import { ApiError } from '../api/client';

interface UseApiState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

interface UseApiReturn<T> extends UseApiState<T> {
  refetch: () => void;
}

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
): UseApiReturn<T> {
  const [state, setState] = useState<UseApiState<T>>({
    data: null,
    loading: true,
    error: null,
  });

  const fetchData = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await fetcher();
      setState({ data, loading: false, error: null });
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `Error ${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : 'Unknown error';
      setState({ data: null, loading: false, error: message });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return { ...state, refetch: fetchData };
}

interface UsePollingOptions {
  interval: number;
  enabled?: boolean;
}

export function usePollingApi<T>(
  fetcher: () => Promise<T>,
  options: UsePollingOptions,
  deps: unknown[] = [],
): UseApiReturn<T> {
  const { interval, enabled = true } = options;
  const result = useApi<T>(fetcher, deps);

  useEffect(() => {
    if (!enabled) return;
    const timer = setInterval(() => {
      result.refetch();
    }, interval);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interval, enabled]);

  return result;
}
