/**
 * FOWT-prevention script. Inlined into <head> via a dangerouslySet
 * <script> in app/layout.tsx so it runs before React hydrates, before
 * the first paint, and decides the theme synchronously.
 *
 * Decision tree:
 *   1. `?theme=light` or `?theme=dark` in the URL — write to
 *      localStorage and use that. Useful for sharing a specific
 *      theme via link and for our screenshot tooling.
 *   2. Otherwise, if `localStorage("marketmind-theme")` is set to
 *      "light" or "dark", respect it.
 *   3. Otherwise, default to "dark" UNLESS the OS reports
 *      prefers-color-scheme: light. (Honest Terminal is the default.)
 *
 * The script is plain string-templated JS — keep it tiny and safe.
 */

export const themeScript = `(function(){try{var k='marketmind-theme';var q=(new URLSearchParams(window.location.search)).get('theme');if(q==='light'||q==='dark'){localStorage.setItem(k,q);}var s=localStorage.getItem(k);var m=window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches;var d=s?s==='dark':!m;if(d){document.documentElement.classList.add('dark');}}catch(e){document.documentElement.classList.add('dark');}})();`;
