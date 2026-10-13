import { createContext, useContext } from "react";

export const FrontierContext = createContext(false);
export const useFrontierWorkspace = () => useContext(FrontierContext);
