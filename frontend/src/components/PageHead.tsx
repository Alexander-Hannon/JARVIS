import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';

/* Page title with a contextual link into the matching Help guide. */
export function PageHead({ title, guide, children }: { title: string; guide?: string; children?: ReactNode }) {
  return (
    <header className="page-head v4-head">
      <div className="page-head-row">
        <h1>{title}</h1>
        {guide && (
          <Link to={`/help#${guide}`} className="help-q" aria-label={`Help: ${title}`} title="How this works">
            ?
          </Link>
        )}
      </div>
      {children && <div className="page-head-sub">{children}</div>}
    </header>
  );
}
