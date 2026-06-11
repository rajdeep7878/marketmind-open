/**
 * Plausible Analytics snippet, rendered only when configured.
 *
 *   NEXT_PUBLIC_PLAUSIBLE_DOMAIN — when set, the site reports to
 *   plausible.io for the given site key. Leaving it unset suppresses
 *   the tag entirely (dev / preview environments).
 *
 *   NEXT_PUBLIC_PLAUSIBLE_SCRIPT_URL — optional override for the
 *   script URL. Defaults to the privacy-friendly `script.exclusions.js`
 *   variant so we can keep /admin/* out of analytics.
 *
 * The component returns `null` rather than an empty wrapper when the
 * env var is missing, so layouts stay free of extraneous DOM.
 */
export function PlausibleAnalytics(): React.ReactElement | null {
  const domain = process.env.NEXT_PUBLIC_PLAUSIBLE_DOMAIN;
  if (!domain) {
    return null;
  }
  const src =
    process.env.NEXT_PUBLIC_PLAUSIBLE_SCRIPT_URL ?? "https://plausible.io/js/script.exclusions.js";
  return (
    <script
      defer
      data-domain={domain}
      data-exclude="/admin/*"
      src={src}
      // eslint-disable-next-line react/no-unknown-property
      data-testid="plausible-script"
    />
  );
}
