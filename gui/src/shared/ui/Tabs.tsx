export interface TabDef<T extends string> {
  id: T;
  label: string;
  count: number;
}

interface TabsProps<T extends string> {
  tabs: readonly TabDef<T>[];
  active: T;
  onChange: (id: T) => void;
}

export function Tabs<T extends string>({ tabs, active, onChange }: TabsProps<T>) {
  return (
    <div className="mfp-tabs" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          aria-selected={tab.id === active}
          data-state={tab.id === active ? "active" : "inactive"}
          className="mfp-tabs__tab"
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
          <span className="mfp-tabs__count">({tab.count})</span>
        </button>
      ))}
    </div>
  );
}
