import { AppSidebar } from './AppSidebar';

interface PageLayoutProps {
  children: React.ReactNode;
  sidebarContent: React.ReactNode;
}

export function PageLayout({ children, sidebarContent }: PageLayoutProps) {
  return (
    <div className="flex h-full w-full">
      <AppSidebar>
        {sidebarContent}
      </AppSidebar>
      <div className="flex-1 overflow-hidden">
        {children}
      </div>
    </div>
  );
}
