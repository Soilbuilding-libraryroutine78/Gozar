import { SpinnerIcon } from "./icons";

/**
 * An accessible inline loading indicator. Announces "Loading" to assistive tech
 * via a visually-hidden label and `role="status"`.
 */
export function Spinner({
  label = "Loading",
  size = 20,
}: {
  readonly label?: string;
  readonly size?: number;
}): JSX.Element {
  return (
    <span className="spinner" role="status">
      <SpinnerIcon className="spinner__icon" size={size} />
      <span className="visually-hidden">{label}</span>
    </span>
  );
}
