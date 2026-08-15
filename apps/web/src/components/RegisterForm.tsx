"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { register, type FormState } from "@/app/(auth)/actions";

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="w-full rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
    >
      {pending ? "Submitting…" : "Request access"}
    </button>
  );
}

function Field({
  label,
  name,
  type = "text",
  required = true,
  autoComplete,
  hint,
  error,
}: {
  label: string;
  name: string;
  type?: string;
  required?: boolean;
  autoComplete?: string;
  hint?: string;
  error?: string;
}) {
  return (
    <div>
      <label htmlFor={name} className="block text-sm font-medium">
        {label}
        {!required ? <span className="ml-1 text-slate-400">(optional)</span> : null}
      </label>
      <input
        id={name}
        name={name}
        type={type}
        required={required}
        autoComplete={autoComplete}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${name}-error` : hint ? `${name}-hint` : undefined}
        className={`mt-1 w-full rounded-lg border px-3 py-2 text-sm outline-none transition focus:ring-2 focus:ring-blue-500 dark:bg-slate-900 ${
          error ? "border-red-400 dark:border-red-700" : "border-slate-300 dark:border-slate-700"
        }`}
      />
      {error ? (
        <p id={`${name}-error`} className="mt-1 text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      ) : hint ? (
        <p id={`${name}-hint`} className="mt-1 text-xs text-slate-500">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function RegisterForm() {
  const [state, formAction] = useActionState<FormState, FormData>(register, {});

  return (
    <form action={formAction} className="space-y-4">
      {state.error ? (
        <p
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
        >
          {state.error}
        </p>
      ) : null}

      <Field label="Name" name="name" autoComplete="name" error={state.fieldErrors?.name} />
      <Field
        label="Email"
        name="email"
        type="email"
        autoComplete="email"
        error={state.fieldErrors?.email}
      />
      <Field
        label="Password"
        name="password"
        type="password"
        autoComplete="new-password"
        hint="At least 12 characters, using two or more of: lowercase, uppercase, digits, symbols."
        error={state.fieldErrors?.password}
      />
      <Field
        label="Phone"
        name="phone"
        type="tel"
        required={false}
        autoComplete="tel"
        error={state.fieldErrors?.phone}
      />

      <Submit />
    </form>
  );
}
