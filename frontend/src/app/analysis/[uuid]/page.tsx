import AnalysisViewer from "./viewer";

export const metadata = { title: "IndiGeo | Stored analysis" };

// Archive records are keyed by the uuid in the URL, so there is no shell worth prerendering.
// Blocking lets the route wait for the param instead of streaming a fallback the client
// would have to replace on hydration.
export const instant = false;

export default async function Page({ params }: { params: Promise<{ uuid: string }> }) {
  const { uuid } = await params;
  return <AnalysisViewer uuid={uuid} />;
}
