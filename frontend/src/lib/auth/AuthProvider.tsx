import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { ApiError } from "../api/client";
import { checkReady, getAgents } from "../api/endpoints";

const TOKEN_KEY = "nexus.api.token";
const BASE_URL_KEY = "nexus.api.baseUrl";

function readStored(key: string): string {
  try {
    return sessionStorage.getItem(key) ?? localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

export type ConnectionState =
  | { kind: "unconfigured" }
  | { kind: "checking" }
  | { kind: "connected"; agentCount: number }
  | { kind: "failed"; message: string };

interface AuthContextValue {
  baseUrl: string;
  token: string;
  connection: ConnectionState;
  setBaseUrl: (url: string) => void;
  saveToken: (token: string) => void;
  clearToken: () => void;
  testConnection: () => Promise<boolean>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function hasStoredToken(): boolean {
  return readStored(TOKEN_KEY).length > 0;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [baseUrl, setBaseUrlState] = useState(() => {
    try {
      return (
        localStorage.getItem(BASE_URL_KEY) ?? window.location.origin
      );
    } catch {
      return window.location.origin;
    }
  });
  const [token, setToken] = useState(() => readStored(TOKEN_KEY));
  const [connection, setConnection] = useState<ConnectionState>(() =>
    hasStoredToken() ? { kind: "checking" } : { kind: "unconfigured" },
  );

  const setBaseUrl = useCallback((url: string) => {
    const trimmed = url.trim().replace(/\/+$/, "");
    setBaseUrlState(trimmed);
    try {
      localStorage.setItem(BASE_URL_KEY, trimmed);
    } catch {
      // Base URL is a public, non-secret preference; ignore write failures.
    }
  }, []);

  const testConnection = useCallback(async (): Promise<boolean> => {
    const current = readStored(TOKEN_KEY);
    const storedBase = (() => {
      try {
        return localStorage.getItem(BASE_URL_KEY) ?? window.location.origin;
      } catch {
        return window.location.origin;
      }
    })();
    if (current.length === 0) {
      setConnection({ kind: "unconfigured" });
      return false;
    }
    setConnection({ kind: "checking" });
    try {
      await checkReady(storedBase);
      const agents = await getAgents(storedBase, current);
      setConnection({ kind: "connected", agentCount: agents.length });
      return true;
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : "The backend is unreachable.";
      setConnection({ kind: "failed", message });
      return false;
    }
  }, []);

  const saveToken = useCallback((next: string) => {
    const trimmed = next.trim();
    // Tab-scoped by default: the bearer token never lands in localStorage.
    try {
      sessionStorage.setItem(TOKEN_KEY, trimmed);
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      // Storage unavailable (private mode); keep it in memory only.
    }
    setToken(trimmed);
    setConnection({ kind: "checking" });
  }, []);

  const clearToken = useCallback(() => {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      // Ignore cleanup failures; memory copy is dropped regardless.
    }
    setToken("");
    setConnection({ kind: "unconfigured" });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      baseUrl,
      token,
      connection,
      setBaseUrl,
      saveToken,
      clearToken,
      testConnection,
    }),
    [baseUrl, token, connection, setBaseUrl, saveToken, clearToken, testConnection],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return value;
}

/** Storage keys are exported for tests only. */
export const __storageKeys = { TOKEN_KEY, BASE_URL_KEY };
