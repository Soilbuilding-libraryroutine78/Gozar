/**
 * Skeleton loaders for list/table loading states. They give a calmer, more
 * "settled" loading impression than a bare spinner while the real data arrives.
 *
 * Accessibility: the skeleton shapes are purely decorative (`aria-hidden`); the
 * surrounding loading region keeps an accessible, visually-hidden `role="status"`
 * announcement so assistive technology still hears that content is loading. The
 * shimmer animation is disabled under prefers-reduced-motion (see styles.css).
 */

/** A single shimmering placeholder bar. */
export function SkeletonBar({ width }: { readonly width?: string }): JSX.Element {
  return (
    <span
      className="skeleton__bar"
      aria-hidden
      style={width !== undefined ? { width } : undefined}
    />
  );
}

/**
 * A table-shaped skeleton with the given number of columns and rows, wrapped in
 * the same `table-wrap` surface used by the real tables so the loading and loaded
 * states share one footprint. `label` is announced to assistive technology.
 */
export function TableSkeleton({
  columns,
  rows = 4,
  label,
}: {
  readonly columns: number;
  readonly rows?: number;
  readonly label: string;
}): JSX.Element {
  return (
    <div className="table-wrap" role="status">
      <span className="visually-hidden">{label}</span>
      <table className="table table--skeleton" aria-hidden>
        <tbody>
          {Array.from({ length: rows }).map((_, rowIndex) => (
            <tr key={rowIndex}>
              {Array.from({ length: columns }).map((__, colIndex) => (
                <td key={colIndex}>
                  <SkeletonBar width={colIndex === 0 ? "70%" : "45%"} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A skeleton made of stacked cards, for card-list loading states (e.g. chains). */
export function CardSkeleton({
  count = 3,
  label,
}: {
  readonly count?: number;
  readonly label: string;
}): JSX.Element {
  return (
    <div className="card-skeleton" role="status">
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: count }).map((_, index) => (
        <div className="card-skeleton__item" aria-hidden key={index}>
          <SkeletonBar width="40%" />
          <SkeletonBar width="65%" />
          <SkeletonBar width="30%" />
        </div>
      ))}
    </div>
  );
}
