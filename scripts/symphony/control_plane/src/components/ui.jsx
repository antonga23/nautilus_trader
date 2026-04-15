import clsx from 'clsx';

export function Button({ className, variant = 'default', ...props }) {
  return <button className={clsx('button', `button-${variant}`, className)} type="button" {...props} />;
}

export function Input(props) {
  return <input className="input" {...props} />;
}

export function Textarea(props) {
  return <textarea className="textarea" {...props} />;
}

export function Select(props) {
  return <select className="select" {...props} />;
}

export function Panel({ title, subtitle, action, children, className }) {
  return (
    <section className={clsx('panel', className)}>
      <div className="panel-header">
        <div>
          <h2>{title}</h2>
          {subtitle ? <p className="subtle">{subtitle}</p> : null}
        </div>
        {action ? <div className="panel-action">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Card({ children, className }) {
  return <article className={clsx('card', className)}>{children}</article>;
}

export function Pill({ children, status = 'unknown' }) {
  return <span className={clsx('pill', `pill-${status}`)}>{children}</span>;
}

export function Empty({ children }) {
  return <div className="empty-state">{children}</div>;
}

export function Table({ columns, rows, empty }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>{rows.length ? rows : <tr><td colSpan={columns.length}>{empty || 'No records.'}</td></tr>}</tbody>
      </table>
    </div>
  );
}
