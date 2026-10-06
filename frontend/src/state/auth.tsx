import { createContext, useContext, type ReactNode } from 'react';
import type { Me, Role } from '../api/types';

// Mirrors security/principal.PERMS. The server enforces every permission; this only decides what the UI shows.
const PERMS: Record<string, Role[]> = {
  use: ['owner', 'admin', 'approver', 'member'],
  'approve.dual': ['owner', 'admin', 'approver'],
  'users.manage': ['owner', 'admin'],
  'users.owner': ['owner'],
  'org.settings': ['owner', 'admin'],
  'org.integrations': ['owner', 'admin'],
  'org.backups': ['owner', 'admin'],
  'org.doctor': ['owner', 'admin'],
  'org.pause': ['owner', 'admin'],
  'audit.read': ['owner', 'admin', 'auditor'],
};

export interface AuthCtx {
  me: Me | null;
  can: (perm: string) => boolean;
  signOut: () => void;
}

const Ctx = createContext<AuthCtx>({ me: null, can: () => true, signOut: () => undefined });

export function AuthProvider({ me, signOut, children }: { me: Me | null; signOut: () => void; children: ReactNode }) {
  // Without a user record (older server / mocked tests) the UI behaves like the single-owner app.
  const can = (perm: string) => !me || (PERMS[perm] ?? []).includes(me.role);
  return <Ctx.Provider value={{ me, can, signOut }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  return useContext(Ctx);
}
