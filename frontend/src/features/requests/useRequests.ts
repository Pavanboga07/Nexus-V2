import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../lib/auth/AuthProvider";
import { useEventBus } from "../../app/EventBus";
import { describeError } from "../../lib/api/client";
import { getRequest, listPendingApprovals, listRequests } from "../../lib/api/endpoints";
import type { RemoteRequest } from "../../lib/api/types";

export type RequestRole = "outbound" | "inbound";

export interface RequestListState {
  items: RemoteRequest[];
  listState: "loading" | "ready" | "error";
  listError: string | null;
  reload: () => void;
}

/** Requests filtered by direction. Refetch is the recovery path: event
 *  payloads hint, REST answers. */
export function useRequestList(role: RequestRole, status?: string): RequestListState {
  const { baseUrl, token } = useAuth();
  const { subscribe } = useEventBus();
  const [items, setItems] = useState<RemoteRequest[]>([]);
  const [listState, setListState] = useState<"loading" | "ready" | "error">("loading");
  const [listError, setListError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setListState("loading");
    setListError(null);
    try {
      const loaded = await listRequests(baseUrl, token, { role, status });
      setItems(loaded);
      setListState("ready");
    } catch (error) {
      setListState("error");
      setListError(describeError(error));
    }
  }, [baseUrl, token, role, status]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => subscribe("requests", () => void load()), [subscribe, load]);

  return { items, listState, listError, reload: () => void load() };
}

export interface PendingState {
  items: RemoteRequest[];
  pendingState: "loading" | "ready" | "error";
  pendingError: string | null;
  reload: () => void;
}

export function usePendingApprovals(): PendingState {
  const { baseUrl, token } = useAuth();
  const { subscribe } = useEventBus();
  const [items, setItems] = useState<RemoteRequest[]>([]);
  const [pendingState, setPendingState] = useState<"loading" | "ready" | "error">("loading");
  const [pendingError, setPendingError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setPendingState("loading");
    setPendingError(null);
    try {
      const loaded = await listPendingApprovals(baseUrl, token);
      setItems(loaded);
      setPendingState("ready");
    } catch (error) {
      setPendingState("error");
      setPendingError(describeError(error));
    }
  }, [baseUrl, token]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => subscribe("approvals", () => void load()), [subscribe, load]);

  return { items, pendingState, pendingError, reload: () => void load() };
}

export interface RequestDetailState {
  item: RemoteRequest | null;
  detailState: "loading" | "ready" | "error";
  detailError: string | null;
  reload: () => void;
}

export function useRequestDetail(requestId: string): RequestDetailState {
  const { baseUrl, token } = useAuth();
  const { subscribe } = useEventBus();
  const [item, setItem] = useState<RemoteRequest | null>(null);
  const [detailState, setDetailState] = useState<"loading" | "ready" | "error">("loading");
  const [detailError, setDetailError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setDetailState("loading");
    setDetailError(null);
    try {
      const loaded = await getRequest(baseUrl, token, requestId);
      setItem(loaded);
      setDetailState("ready");
    } catch (error) {
      setDetailState("error");
      setDetailError(describeError(error));
    }
  }, [baseUrl, token, requestId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => subscribe("requests", () => void load()), [subscribe, load]);

  return { item, detailState, detailError, reload: () => void load() };
}
