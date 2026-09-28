import { api } from '../api/client';
import { Async, Badge, Card, Empty } from '../components/ui';
import { textOf } from '../lib/format';
import { useAsync } from '../lib/hooks';

export function ProjectsPage() {
  const projects = useAsync(() => api.projects(), []);
  return (
    <div className="page">
      <header className="page-head">
        <h1>Projects</h1>
      </header>
      <Async state={projects} empty="No projects yet.">
        {(list) => {
          const areas = [...new Set(list.map((p) => p.area || 'Other'))];
          return (
            <>
              {areas.map((area) => (
                <section key={area} className="area">
                  <h2 className="area-title">{area}</h2>
                  <div className="grid-cards">
                    {list
                      .filter((p) => (p.area || 'Other') === area)
                      .map((p) => (
                        <Card
                          key={p.id}
                          title={p.name}
                          actions={
                            <>
                              {p.sensitive && <Badge tone="warn">sensitive</Badge>}
                              <Badge tone="neutral">{p.memories_count} memories</Badge>
                            </>
                          }
                        >
                          {p.description ? <p>{p.description}</p> : <p className="muted">No description.</p>}
                          <h3 className="subhead">Next actions</h3>
                          {p.next_actions.length ? (
                            <ul className="list">
                              {p.next_actions.map((a, i) => (
                                <li key={i} className="list-row">
                                  {textOf(a)}
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <Empty>No next actions recorded.</Empty>
                          )}
                          {p.notion_url && (
                            <a href={p.notion_url} target="_blank" rel="noopener noreferrer" className="ext-link">
                              Open in Notion ↗
                            </a>
                          )}
                        </Card>
                      ))}
                  </div>
                </section>
              ))}
            </>
          );
        }}
      </Async>
    </div>
  );
}
