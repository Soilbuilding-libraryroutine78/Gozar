import type { ComponentType } from "react";

interface IconProps {
  readonly size?: number;
  readonly "aria-hidden"?: boolean;
}

export interface PageGuideStep {
  readonly title: string;
  readonly description: string;
  readonly Icon: ComponentType<IconProps>;
}

interface PageGuideProps {
  readonly id: string;
  readonly title: string;
  readonly description: string;
  readonly steps: ReadonlyArray<PageGuideStep>;
}

/** Compact, reusable operator guide for each console page. */
export function PageGuide({
  id,
  title,
  description,
  steps,
}: PageGuideProps): JSX.Element {
  return (
    <section className="page-guide" aria-labelledby={id}>
      <div className="page-guide__head">
        <span className="page-guide__eyebrow">How this works</span>
        <h2 id={id}>{title}</h2>
        <p>{description}</p>
      </div>
      <ol className="page-guide__steps">
        {steps.map((step) => (
          <li key={step.title} className="page-guide__step">
            <span className="page-guide__icon">
              <step.Icon size={20} aria-hidden />
            </span>
            <span className="page-guide__copy">
              <strong>{step.title}</strong>
              <span>{step.description}</span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
