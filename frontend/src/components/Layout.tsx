import { NavLink, Outlet } from 'react-router';
import { useDashboardStore } from '../lib/store';

/**
 * Minimal shell for the wake-triggered dashboard. The window is hidden by
 * default — when the user sees this layout, Hope is (or recently was) awake.
 */
export function Layout() {
  const bridgeConnected = useDashboardStore((s) => s.bridgeConnected);
  const bridgeError = useDashboardStore((s) => s.bridgeError);

  return (
    <div
      className="flex flex-col h-full w-full overflow-hidden"
      style={{ background: 'var(--color-bg)' }}
    >
      <header
        className="flex items-center gap-4 px-5 py-2 shrink-0 text-sm"
        style={{
          background: 'var(--color-sidebar)',
          borderBottom: '1px solid var(--color-border)',
        }}
      >
        <div
          className="font-semibold tracking-tight"
          style={{ color: 'var(--color-text)' }}
        >
          Hope
        </div>
        <nav className="flex items-center gap-2">
          <LayoutTab to="/">Dashboard</LayoutTab>
          <LayoutTab to="/logs">Logs</LayoutTab>
        </nav>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <span
            aria-hidden="true"
            className="w-1.5 h-1.5 rounded-full"
            style={{
              background: bridgeConnected
                ? 'var(--color-success, #5fd39a)'
                : 'var(--color-error, #e76f6f)',
            }}
          />
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {bridgeConnected ? 'bridge connected' : bridgeError || 'bridge offline'}
          </span>
        </div>
      </header>

      <main className="flex-1 min-h-0 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}

function LayoutTab({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end
      className="px-2.5 py-1 rounded-md text-xs transition-colors"
      style={({ isActive }) => ({
        color: isActive ? 'var(--color-text)' : 'var(--color-text-secondary)',
        background: isActive ? 'var(--color-bg-secondary)' : 'transparent',
        fontWeight: isActive ? 500 : 400,
      })}
    >
      {children}
    </NavLink>
  );
}
