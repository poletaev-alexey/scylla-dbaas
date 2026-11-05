import { useEffect, useRef, type ReactNode } from "react";
import css from "./Dialog.module.css";

/** Modal built on the native <dialog> element (focus trap and Esc handling for free). */
export function Dialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
    return () => {
      d?.close();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={css.dialog}
      aria-labelledby="dialog-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <header className={css.header}>
        <h2 id="dialog-title">{title}</h2>
        <button type="button" className="ghost" aria-label="Close" onClick={onClose}>
          ✕
        </button>
      </header>
      {children}
    </dialog>
  );
}
