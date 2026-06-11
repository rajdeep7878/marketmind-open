import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Editorial table primitives. Apply `editorial` class to <table>; the
 * actual styling lives in globals.css so we don't repeat the hairline
 * + tabular-num rules at every site.
 */

function Table({
  className,
  ...props
}: React.TableHTMLAttributes<HTMLTableElement>): React.ReactElement {
  return <table className={cn("editorial", className)} {...props} />;
}

function TableHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLTableSectionElement>): React.ReactElement {
  return <thead className={className} {...props} />;
}

function TableBody({
  className,
  ...props
}: React.HTMLAttributes<HTMLTableSectionElement>): React.ReactElement {
  return <tbody className={className} {...props} />;
}

function TableRow({
  className,
  ...props
}: React.HTMLAttributes<HTMLTableRowElement>): React.ReactElement {
  return <tr className={className} {...props} />;
}

function TableHead({
  className,
  ...props
}: React.ThHTMLAttributes<HTMLTableCellElement>): React.ReactElement {
  return <th className={className} {...props} />;
}

function TableCell({
  className,
  numeric = false,
  ...props
}: React.TdHTMLAttributes<HTMLTableCellElement> & { numeric?: boolean }): React.ReactElement {
  return <td className={cn(numeric && "num", className)} {...props} />;
}

export { Table, TableBody, TableCell, TableHead, TableHeader, TableRow };
