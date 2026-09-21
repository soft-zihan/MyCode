import { useState, useEffect } from 'react';
import { 
  fetchAgents, fetchAgent, fetchConfig, saveConfig, fetchSkills,
  Agent, AgentDetail, AppConfig, ModelEndpointConfig, Skill,
  DEFAULT_CONTEXT_WINDOW, DEFAULT_AUTO_COMPACT_THRESHOLD
} from '../api/client';
import { Bot, Server, RefreshCw, Save, Plus, Trash2, ChevronDown, ChevronRight, Globe, Cpu, CheckCircle, XCircle, Loader, Edit2, X, RotateCcw, Sparkles } from 'lucide-react';
import { PageLayout } from '../components/PageLayout';

interface ApiProvider {
  id: string;
  name: string;
  base_url: string;
  api_key: string;
  models: ModelConfig[];
}

interface ModelConfig {
  id: string;
  name: string;
  context_window: number;
  auto_compact_threshold: number;
}

interface ToolInfo {
  name: string;
  description: string;
  deferred: boolean;
  source: 'native' | 'mcp';
}

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<AgentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [activeSection, setActiveSection] = useState<'models' | 'agents'>('agents');
  const [providers, setProviders] = useState<ApiProvider[]>([]);
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [verifyingModel, setVerifyingModel] = useState<string | null>(null);
  const [verifyResults, setVerifyResults] = useState<Record<string, { status: string; message: string }>>({});
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [editedPrompt, setEditedPrompt] = useState<string>('');
  const [availableTools, setAvailableTools] = useState<ToolInfo[]>([]);
  const [showCreateAgent, setShowCreateAgent] = useState(false);
  const [newAgent, setNewAgent] = useState<{name: string; description: string; system_prompt: string; allowed_tools: string[]; model: string}>({
    name: '',
    description: '',
    system_prompt: '',
    allowed_tools: [],
    model: '',
  });
  const [editingTools, setEditingTools] = useState(false);
  const [tempAllowedTools, setTempAllowedTools] = useState<string[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [agentsData, configData, toolsData, skillsData] = await Promise.all([
        fetchAgents(),
        fetchConfig(),
        fetch('/api/tools/all').then(r => r.json()),
        fetchSkills(),
      ]);
      setAgents(agentsData);
      setConfig(configData);
      setAvailableTools(toolsData);
      setSkills(skillsData);
      
      const providerMap = new Map<string, ApiProvider>();
      Object.entries(configData.endpoints).forEach(([id, endpoint]) => {
        const key = `${endpoint.base_url}|${endpoint.api_key}`;
        if (!providerMap.has(key)) {
          const providerName = endpoint.provider_name || new URL(endpoint.base_url).hostname;
          providerMap.set(key, {
            id: `provider_${providerMap.size}`,
            name: providerName,
            base_url: endpoint.base_url,
            api_key: endpoint.api_key,
            models: []
          });
        }
        const provider = providerMap.get(key)!;
        provider.models.push({
          id: id,
          name: endpoint.model,
          context_window: endpoint.context_window || DEFAULT_CONTEXT_WINDOW,
          auto_compact_threshold: endpoint.auto_compact_threshold ?? DEFAULT_AUTO_COMPACT_THRESHOLD
        });
      });
      setProviders(Array.from(providerMap.values()));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load data');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleAgentClick = async (agentName: string) => {
    setActiveSection('agents');
    try {
      const detail = await fetchAgent(agentName);
      setSelectedAgent(detail);
      setEditingPrompt(false);
      setEditingTools(false);
    } catch (err) {
      console.error('Failed to load agent detail:', err);
    }
  };

  const handleProviderClick = (providerId: string) => {
    setActiveSection('models');
    setExpandedProvider(expandedProvider === providerId ? null : providerId);
  };

  const handleEditPrompt = async () => {
    if (!selectedAgent) return;
    try {
      const response = await fetch(`/api/agents/${selectedAgent.name}/prompt`);
      if (response.ok) {
        const data = await response.json();
        setEditedPrompt(data.prompt || '');
        setEditingPrompt(true);
      } else {
        alert('Failed to load prompt');
      }
    } catch (err) {
      console.error('Failed to load prompt:', err);
      alert('Failed to load prompt');
    }
  };

  const handleSavePrompt = async () => {
    if (!selectedAgent) return;
    try {
      const response = await fetch(`/api/agents/${selectedAgent.name}/prompt`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: editedPrompt }),
      });
      if (response.ok) {
        setEditingPrompt(false);
        const detail = await fetchAgent(selectedAgent.name);
        setSelectedAgent(detail);
      } else {
        alert('Failed to save prompt');
      }
    } catch (err) {
      console.error('Failed to save prompt:', err);
      alert('Failed to save prompt');
    }
  };

  const handleStartEditTools = () => {
    if (!selectedAgent) return;
    const currentTools = selectedAgent.custom_config?.allowed_tools || [];
    setTempAllowedTools(currentTools);
    setEditingTools(true);
  };

  const handleToggleTool = (toolName: string) => {
    setTempAllowedTools(prev => 
      prev.includes(toolName)
        ? prev.filter(t => t !== toolName)
        : [...prev, toolName]
    );
  };

  const handleSaveTools = async () => {
    if (!selectedAgent) return;
    try {
      const response = await fetch(`/api/agents/${selectedAgent.name}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          allowed_tools: tempAllowedTools.length > 0 ? tempAllowedTools : null,
        }),
      });
      if (response.ok) {
        setEditingTools(false);
        const detail = await fetchAgent(selectedAgent.name);
        setSelectedAgent(detail);
      } else {
        alert('Failed to save tools');
      }
    } catch (err) {
      console.error('Failed to save tools:', err);
      alert('Failed to save tools');
    }
  };

  const handleCreateAgent = async () => {
    if (!newAgent.name || !newAgent.description || !newAgent.system_prompt) {
      alert('Please fill in all required fields');
      return;
    }
    try {
      const response = await fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: newAgent.name,
          description: newAgent.description,
          system_prompt: newAgent.system_prompt,
          allowed_tools: newAgent.allowed_tools.length > 0 ? newAgent.allowed_tools : [],
          model: newAgent.model || null,
        }),
      });
      if (response.ok) {
        setShowCreateAgent(false);
        setNewAgent({ name: '', description: '', system_prompt: '', allowed_tools: [], model: '' });
        await loadData();
      } else {
        const error = await response.json();
        alert(`Failed to create agent: ${error.detail || 'Unknown error'}`);
      }
    } catch (err) {
      console.error('Failed to create agent:', err);
      alert('Failed to create agent');
    }
  };

  const handleDeleteAgent = async (agentName: string) => {
    const isBuiltin = agentName === 'explore' || agentName === 'general';
    const confirmMsg = isBuiltin 
      ? `Reset "${agentName}" to default? This removes your customizations.`
      : `Delete agent "${agentName}"?`;
    
    if (!confirm(confirmMsg)) return;
    try {
      const response = await fetch(`/api/agents/${agentName}`, { method: 'DELETE' });
      if (response.ok) {
        if (selectedAgent?.name === agentName) {
          setSelectedAgent(null);
        }
        await loadData();
      } else {
        const error = await response.json();
        alert(`Failed: ${error.detail || 'Unknown error'}`);
      }
    } catch (err) {
      console.error('Failed:', err);
      alert('Failed');
    }
  };

  const handleSaveConfig = async () => {
    if (!config) return;
    setSaving(true);
    try {
      const endpoints: Record<string, ModelEndpointConfig> = {};
      providers.forEach(provider => {
        provider.models.forEach(model => {
          endpoints[model.id] = {
            model: model.name,
            base_url: provider.base_url,
            api_key: provider.api_key,
            context_window: model.context_window,
            auto_compact_threshold: model.auto_compact_threshold,
            provider_name: provider.name
          };
        });
      });
      
      const newConfig = { ...config, endpoints };
      await saveConfig(newConfig);
      
      const reloadedConfig = await fetchConfig();
      setConfig(reloadedConfig);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to save config');
    } finally {
      setSaving(false);
    }
  };

  const addProvider = () => {
    const newProvider: ApiProvider = {
      id: `provider_${Date.now()}`,
      name: 'New API Provider',
      base_url: 'https://api.openai.com/v1',
      api_key: '',
      models: []
    };
    setProviders([...providers, newProvider]);
    setExpandedProvider(newProvider.id);
  };

  const updateProvider = (id: string, field: keyof ApiProvider, value: any) => {
    setProviders(providers.map(p => 
      p.id === id ? { ...p, [field]: value } : p
    ));
  };

  const deleteProvider = (id: string) => {
    if (!confirm('Delete this API provider and all its models?')) return;
    setProviders(providers.filter(p => p.id !== id));
  };

  const addModel = (providerId: string) => {
    setProviders(providers.map(p => {
      if (p.id === providerId) {
        return {
          ...p,
          models: [...p.models, {
            id: `model_${Date.now()}`,
            name: 'new-model',
            context_window: DEFAULT_CONTEXT_WINDOW,
            auto_compact_threshold: DEFAULT_AUTO_COMPACT_THRESHOLD
          }]
        };
      }
      return p;
    }));
  };

  const updateModel = (providerId: string, modelId: string, field: keyof ModelConfig, value: any) => {
    setProviders(providers.map(p => {
      if (p.id === providerId) {
        return {
          ...p,
          models: p.models.map(m => 
            m.id === modelId ? { ...m, [field]: value } : m
          )
        };
      }
      return p;
    }));
  };

  const deleteModel = (providerId: string, modelId: string) => {
    setProviders(providers.map(p => {
      if (p.id === providerId) {
        return {
          ...p,
          models: p.models.filter(m => m.id !== modelId)
        };
      }
      return p;
    }));
  };

  const updateAgentRouting = (agentName: string, modelId: string) => {
    if (!config) return;
    setConfig({
      ...config,
      routing: { ...config.routing, [agentName]: modelId }
    });
  };

  const getAllModels = () => {
    const models: { id: string; name: string; provider: string }[] = [];
    providers.forEach(p => {
      p.models.forEach(m => {
        models.push({ id: m.id, name: m.name, provider: p.name });
      });
    });
    return models;
  };

  const verifyModel = async (providerId: string, modelId: string) => {
    const provider = providers.find(p => p.id === providerId);
    const model = provider?.models.find(m => m.id === modelId);
    
    if (!provider || !model) return;
    
    setVerifyingModel(modelId);
    try {
      const response = await fetch('/api/models/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          base_url: provider.base_url,
          api_key: provider.api_key,
          model: model.name
        })
      });
      
      const result = await response.json();
      setVerifyResults(prev => ({
        ...prev,
        [modelId]: { status: result.status, message: result.message }
      }));
    } catch (err) {
      setVerifyResults(prev => ({
        ...prev,
        [modelId]: { 
          status: 'error', 
          message: err instanceof Error ? err.message : 'Verification failed' 
        }
      }));
    } finally {
      setVerifyingModel(null);
    }
  };

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-gray-500">Loading...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-red-500">{error}</div>
      </div>
    );
  }

  // Filter tools based on agent type restrictions
  const getAvailableToolsForAgent = (agentName: string, agentCategory: string) => {
    let tools = availableTools.filter(t => t.name !== 'agent');
    
    // Sub-agents have specific tool restrictions
    if (agentCategory === 'sub') {
      if (agentName === 'explore') {
        // explore: only read-only tools
        tools = tools.filter(t => ['read_file', 'outline_file', 'list_files', 'grep_search'].includes(t.name));
      } else if (agentName === 'reviewer') {
        // reviewer: read-only + run_shell
        tools = tools.filter(t => ['read_file', 'outline_file', 'list_files', 'grep_search', 'run_shell'].includes(t.name));
      }
      // general: all tools except agent (already filtered)
    }
    
    return tools;
  };
  
  const allToolsForConfig = availableTools.filter(t => t.name !== 'agent');

  const sidebarContent = (
    <div className="flex flex-col h-full">
      <div className="border-b border-gray-200 bg-indigo-50/30 h-1/3 flex flex-col">
        <div className="px-3 py-2 flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            <Server className="w-3.5 h-3.5 text-indigo-500" />
            <h3 className="text-xs font-semibold text-gray-700">Providers</h3>
          </div>
          <span className="text-[10px] text-gray-400">{providers.length}</span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {providers.length === 0 ? (
            <div className="px-3 py-2 text-xs text-gray-400">No providers</div>
          ) : (
            providers.map(provider => (
              <div key={provider.id}>
                <div
                  className={`px-3 py-1.5 cursor-pointer hover:bg-indigo-100/50 transition-colors flex items-center gap-1.5 ${
                    activeSection === 'models' && expandedProvider === provider.id ? 'bg-indigo-100 border-l-2 border-indigo-500' : ''
                  }`}
                  onClick={() => handleProviderClick(provider.id)}
                >
                  <Globe className="w-3 h-3 text-gray-400 flex-shrink-0" />
                  <span className="text-xs text-gray-700 truncate flex-1">{provider.name}</span>
                  <span className="text-[10px] text-gray-400">{provider.models.length}</span>
                </div>
                {expandedProvider === provider.id && provider.models.length > 0 && (
                  <div className="bg-indigo-50/50 border-t border-indigo-100">
                    {provider.models.map(model => (
                      <div key={model.id} className="px-3 py-1 pl-7 text-[11px] text-gray-500 truncate">
                        {model.name}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto bg-purple-50/30 flex flex-col">
        <div className="px-3 py-2 flex items-center justify-between border-b border-gray-100">
          <div className="flex items-center gap-1.5">
            <Bot className="w-3.5 h-3.5 text-purple-500" />
            <h3 className="text-xs font-semibold text-gray-700">Agents</h3>
          </div>
        </div>
        
        {/* Primary Agents */}
        {agents.filter(a => a.category === 'primary').length > 0 && (
          <div className="border-b border-gray-100">
            <div className="px-3 py-1 text-[10px] font-medium text-gray-500 uppercase bg-purple-50/50">Primary</div>
            {agents.filter(a => a.category === 'primary').map(agent => (
              <div
                key={agent.name}
                className={`px-3 py-1.5 cursor-pointer hover:bg-purple-100/50 transition-colors flex items-center gap-1.5 ${
                  activeSection === 'agents' && selectedAgent?.name === agent.name ? 'bg-purple-100 border-l-2 border-purple-500' : ''
                }`}
                onClick={() => handleAgentClick(agent.name)}
              >
                <Bot className="w-3 h-3 text-indigo-500 flex-shrink-0" />
                <span className="text-xs text-gray-700 truncate flex-1">{agent.name}</span>
                {agent.has_override && (
                  <span className="text-[9px] px-1 py-0.5 bg-amber-100 text-amber-700 rounded" title="Has custom override">~</span>
                )}
              </div>
            ))}
          </div>
        )}
        
        {/* Sub Agents */}
        <div className="border-b border-gray-100">
          <div className="px-3 py-1 flex items-center justify-between bg-purple-50/50">
            <span className="text-[10px] font-medium text-gray-500 uppercase">Sub Agents</span>
            <button
              onClick={() => setShowCreateAgent(true)}
              className="p-0.5 text-gray-400 hover:text-gray-600 hover:bg-purple-100 rounded transition-colors"
              title="Create sub agent"
            >
              <Plus className="w-3.5 h-3.5" />
            </button>
          </div>
          {agents.filter(a => a.category === 'sub').map(agent => (
            <div
              key={agent.name}
              className={`px-3 py-1.5 cursor-pointer hover:bg-purple-100/50 transition-colors flex items-center gap-1.5 ${
                activeSection === 'agents' && selectedAgent?.name === agent.name ? 'bg-purple-100 border-l-2 border-purple-500' : ''
              }`}
              onClick={() => handleAgentClick(agent.name)}
            >
              <Bot className="w-3 h-3 text-green-500 flex-shrink-0" />
              <span className="text-xs text-gray-700 truncate flex-1">{agent.name}</span>
              {agent.has_override && (
                <span className="text-[9px] px-1 py-0.5 bg-amber-100 text-amber-700 rounded" title="Has custom override">~</span>
              )}
            </div>
          ))}
          {/* Custom agents also go here */}
          {agents.filter(a => a.category === 'custom').map(agent => (
            <div
              key={agent.name}
              className={`px-3 py-1.5 cursor-pointer hover:bg-purple-100/50 transition-colors flex items-center gap-1.5 ${
                activeSection === 'agents' && selectedAgent?.name === agent.name ? 'bg-purple-100 border-l-2 border-purple-500' : ''
              }`}
              onClick={() => handleAgentClick(agent.name)}
            >
              <Bot className="w-3 h-3 text-purple-500 flex-shrink-0" />
              <span className="text-xs text-gray-700 truncate flex-1">{agent.name}</span>
            </div>
          ))}
        </div>
        
        {/* Hidden Agents */}
        {agents.filter(a => a.category === 'hidden').length > 0 && (
          <div>
            <div className="px-3 py-1 text-[10px] font-medium text-gray-500 uppercase bg-purple-50/50">Hidden</div>
            {agents.filter(a => a.category === 'hidden').map(agent => (
              <div
                key={agent.name}
                className={`px-3 py-1.5 cursor-pointer hover:bg-purple-100/50 transition-colors flex items-center gap-1.5 ${
                  activeSection === 'agents' && selectedAgent?.name === agent.name ? 'bg-purple-100 border-l-2 border-purple-500' : ''
                }`}
                onClick={() => handleAgentClick(agent.name)}
              >
                <Bot className="w-3 h-3 text-gray-400 flex-shrink-0" />
                <span className="text-xs text-gray-700 truncate flex-1">{agent.name}</span>
                {agent.has_override && (
                  <span className="text-[9px] px-1 py-0.5 bg-amber-100 text-amber-700 rounded" title="Has custom override">~</span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );

  return (
    <PageLayout sidebarContent={sidebarContent}>
      <div className="h-full flex flex-col bg-white">
        {activeSection === 'models' ? (
          <>
            <div className="p-6 border-b border-gray-200">
              <div className="flex items-center justify-between">
                <div>
                  <h1 className="text-2xl font-bold text-gray-900">Model Providers</h1>
                  <p className="text-sm text-gray-500 mt-1">
                    {providers.length} provider{providers.length !== 1 ? 's' : ''} • {getAllModels().length} model{getAllModels().length !== 1 ? 's' : ''}
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={loadData}
                    className="flex items-center px-4 py-2 bg-gray-500 text-white rounded hover:bg-gray-600 transition-colors"
                  >
                    <RefreshCw className="w-4 h-4 mr-2" />
                    Refresh
                  </button>
                  <button
                    onClick={handleSaveConfig}
                    disabled={saving || !config}
                    className="flex items-center px-4 py-2 bg-indigo-500 text-white rounded hover:bg-indigo-600 transition-colors disabled:opacity-50"
                  >
                    <Save className="w-4 h-4 mr-2" />
                    {saving ? 'Saving...' : 'Save Config'}
                  </button>
                </div>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-6">
              {providers.length === 0 ? (
                <div className="text-center py-12 text-gray-500">
                  <Server className="w-12 h-12 mx-auto mb-4 opacity-50" />
                  <p>No API providers configured</p>
                  <button
                    onClick={addProvider}
                    className="mt-4 flex items-center px-4 py-2 bg-green-500 text-white rounded hover:bg-green-600 transition-colors mx-auto"
                  >
                    <Plus className="w-4 h-4 mr-2" />
                    Add Provider
                  </button>
                </div>
              ) : (
                <div className="space-y-4">
                  {providers.map(provider => (
                    <div key={provider.id} className="border border-gray-200 rounded-lg overflow-hidden">
                      <div 
                        className="flex items-center justify-between p-4 bg-gray-50 cursor-pointer hover:bg-gray-100"
                        onClick={() => setExpandedProvider(expandedProvider === provider.id ? null : provider.id)}
                      >
                        <div className="flex items-center gap-3">
                          {expandedProvider === provider.id ? (
                            <ChevronDown className="w-5 h-5 text-gray-500" />
                          ) : (
                            <ChevronRight className="w-5 h-5 text-gray-500" />
                          )}
                          <Globe className="w-5 h-5 text-indigo-500" />
                          <div>
                            <div className="font-semibold text-gray-900">{provider.name}</div>
                            <div className="text-xs text-gray-500">{provider.base_url}</div>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-sm text-gray-600">{provider.models.length} model{provider.models.length !== 1 ? 's' : ''}</span>
                          <button
                            onClick={(e) => { e.stopPropagation(); deleteProvider(provider.id); }}
                            className="p-1 text-red-500 hover:bg-red-50 rounded"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </div>
                      </div>

                      {expandedProvider === provider.id && (
                        <div className="p-4 border-t border-gray-200">
                          <div className="space-y-3 mb-4">
                            <div>
                              <label className="block text-xs font-medium text-gray-600 mb-1">Provider Name</label>
                              <input
                                type="text"
                                value={provider.name}
                                onChange={(e) => updateProvider(provider.id, 'name', e.target.value)}
                                className="w-full px-3 py-2 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                              />
                            </div>
                            <div>
                              <label className="block text-xs font-medium text-gray-600 mb-1">Base URL</label>
                              <input
                                type="text"
                                value={provider.base_url}
                                onChange={(e) => updateProvider(provider.id, 'base_url', e.target.value)}
                                className="w-full px-3 py-2 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                              />
                            </div>
                            <div>
                              <label className="block text-xs font-medium text-gray-600 mb-1">API Key</label>
                              <input
                                type="password"
                                value={provider.api_key}
                                onChange={(e) => updateProvider(provider.id, 'api_key', e.target.value)}
                                className="w-full px-3 py-2 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                              />
                            </div>
                          </div>

                          <div className="border-t border-gray-200 pt-4">
                            <div className="flex items-center justify-between mb-3">
                              <h4 className="text-sm font-semibold text-gray-700">Models</h4>
                              <button
                                onClick={() => addModel(provider.id)}
                                className="flex items-center px-2 py-1 text-xs bg-indigo-500 text-white rounded hover:bg-indigo-600"
                              >
                                <Plus className="w-3 h-3 mr-1" />
                                Add Model
                              </button>
                            </div>

                            {provider.models.length === 0 ? (
                              <div className="text-sm text-gray-500 text-center py-4">No models configured</div>
                            ) : (
                              <div className="space-y-2">
                                {provider.models.map(model => (
                                  <div key={model.id} className="space-y-2">
                                    <div className="flex items-center gap-2 p-2 bg-white border border-gray-200 rounded">
                                      <Cpu className="w-4 h-4 text-gray-400" />
                                      <input
                                        type="text"
                                        value={model.name}
                                        onChange={(e) => updateModel(provider.id, model.id, 'name', e.target.value)}
                                        className="flex-1 px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                                      />
                                      <input
                                        type="number"
                                        value={model.context_window}
                                        onChange={(e) => updateModel(provider.id, model.id, 'context_window', parseInt(e.target.value) || DEFAULT_CONTEXT_WINDOW)}
                                        className="w-28 px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                                      />
                                      <span className="text-xs text-gray-500">tokens</span>
                                      <input
                                        type="number"
                                        step="0.05"
                                        min="0.1"
                                        max="0.95"
                                        value={model.auto_compact_threshold}
                                        onChange={(e) => updateModel(provider.id, model.id, 'auto_compact_threshold', parseFloat(e.target.value) || DEFAULT_AUTO_COMPACT_THRESHOLD)}
                                        className="w-20 px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                                      />
                                      <span className="text-xs text-gray-500">compact</span>
                                      <button
                                        onClick={() => verifyModel(provider.id, model.id)}
                                        disabled={verifyingModel === model.id}
                                        className="flex items-center px-2 py-1 text-xs bg-indigo-500 text-white rounded hover:bg-indigo-600 disabled:opacity-50"
                                      >
                                        {verifyingModel === model.id ? (
                                          <Loader className="w-3 h-3 animate-spin" />
                                        ) : (
                                          <CheckCircle className="w-3 h-3" />
                                        )}
                                        <span className="ml-1">Test</span>
                                      </button>
                                      <button
                                        onClick={() => deleteModel(provider.id, model.id)}
                                        className="p-1 text-red-500 hover:bg-red-50 rounded"
                                      >
                                        <Trash2 className="w-3 h-3" />
                                      </button>
                                    </div>
                                    {verifyResults[model.id] && (
                                      <div className={`flex items-start gap-2 p-2 rounded text-xs ${
                                        verifyResults[model.id].status === 'success' 
                                          ? 'bg-green-50 text-green-800 border border-green-200'
                                          : 'bg-red-50 text-red-800 border border-red-200'
                                      }`}>
                                        {verifyResults[model.id].status === 'success' ? (
                                          <CheckCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                                        ) : (
                                          <XCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                                        )}
                                        <div className="flex-1">
                                          <div className="font-medium">{verifyResults[model.id].message}</div>
                                        </div>
                                      </div>
                                    )}
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  ))}
                  <button
                    onClick={addProvider}
                    className="w-full flex items-center justify-center px-4 py-3 border-2 border-dashed border-gray-300 rounded-lg text-gray-500 hover:border-green-400 hover:text-green-600 transition-colors"
                  >
                    <Plus className="w-4 h-4 mr-2" />
                    Add Provider
                  </button>
                </div>
              )}
            </div>
          </>
        ) : (
          <>
            <div className="p-6 border-b border-gray-200">
              <div className="flex items-center justify-between">
                <div>
                  <h1 className="text-2xl font-bold text-gray-900">Agents</h1>
                  <p className="text-sm text-gray-500 mt-1">
                    {agents.length} agent{agents.length !== 1 ? 's' : ''}
                  </p>
                </div>
                <button
                  onClick={loadData}
                  className="flex items-center px-4 py-2 bg-gray-500 text-white rounded hover:bg-gray-600 transition-colors"
                >
                  <RefreshCw className="w-4 h-4 mr-2" />
                  Refresh
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-6">
              {selectedAgent ? (
                <div className="max-w-3xl space-y-6">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <Bot className="w-8 h-8 text-purple-500" />
                      <div>
                        <div className="flex items-center gap-2">
                          <h2 className="text-xl font-bold text-gray-900">{selectedAgent.name}</h2>
                          {selectedAgent.has_override && (
                            <span className="px-2 py-0.5 text-xs bg-amber-100 text-amber-700 rounded">Customized</span>
                          )}
                        </div>
                        <p className="text-sm text-gray-500">
                          {agents.find(a => a.name === selectedAgent.name)?.description}
                        </p>
                      </div>
                    </div>
                    {(selectedAgent.is_custom || selectedAgent.has_override) && (
                      <button
                        onClick={() => handleDeleteAgent(selectedAgent.name)}
                        className={`flex items-center px-3 py-1.5 text-sm rounded transition-colors ${
                          selectedAgent.is_custom
                            ? 'bg-red-500 text-white hover:bg-red-600'
                            : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                        }`}
                      >
                        {selectedAgent.is_custom ? (
                          <>
                            <Trash2 className="w-4 h-4 mr-1" />
                            Delete
                          </>
                        ) : (
                          <>
                            <RotateCcw className="w-4 h-4 mr-1" />
                            Reset to Default
                          </>
                        )}
                      </button>
                    )}
                  </div>

                  {/* Model Routing - 只对非 hidden agents 显示 */}
                  {selectedAgent.category !== 'hidden' && (
                    <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                      <h3 className="text-sm font-semibold text-gray-700 mb-2">Model Routing</h3>
                      <select
                        value={config?.routing?.[selectedAgent.name] || ''}
                        onChange={(e) => {
                          updateAgentRouting(selectedAgent.name, e.target.value);
                        }}
                        onBlur={() => {
                          handleSaveConfig();
                        }}
                        className="w-full px-3 py-2 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500 bg-white"
                      >
                        {/* Primary agents don't inherit, they use the default model */}
                        {selectedAgent.category !== 'primary' && (
                          <option value="">Default (inherit from parent)</option>
                        )}
                        {getAllModels().map(m => (
                          <option key={m.id} value={m.id}>
                            {m.name} ({m.provider})
                          </option>
                        ))}
                      </select>
                    </div>
                  )}

                  <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                    <div className="flex items-center justify-between mb-2">
                      <h3 className="text-sm font-semibold text-gray-700">System Prompt</h3>
                      {editingPrompt ? (
                        <div className="flex gap-2">
                          <button
                            onClick={handleSavePrompt}
                            className="flex items-center px-2 py-1 text-xs bg-green-500 text-white rounded hover:bg-green-600"
                          >
                            <Save className="w-3 h-3 mr-1" />
                            Save
                          </button>
                          <button
                            onClick={() => setEditingPrompt(false)}
                            className="flex items-center px-2 py-1 text-xs bg-gray-500 text-white rounded hover:bg-gray-600"
                          >
                            <X className="w-3 h-3 mr-1" />
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={handleEditPrompt}
                          className="flex items-center px-2 py-1 text-xs bg-indigo-500 text-white rounded hover:bg-indigo-600"
                        >
                          <Edit2 className="w-3 h-3 mr-1" />
                          Edit
                        </button>
                      )}
                    </div>
                    {editingPrompt ? (
                      <textarea
                        value={editedPrompt}
                        onChange={(e) => setEditedPrompt(e.target.value)}
                        className="w-full h-64 px-3 py-2 text-xs font-mono border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500 resize-none bg-white"
                      />
                    ) : (
                      <pre className="text-xs font-mono text-gray-700 whitespace-pre-wrap bg-white p-3 rounded border border-gray-200 max-h-48 overflow-y-auto">
                        {selectedAgent.system_prompt_preview || '(No system prompt)'}
                      </pre>
                    )}
                  </div>

                  {/* Allowed Tools - 只对非 hidden agents 显示 */}
                  {selectedAgent.category !== 'hidden' && (
                    <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                      <div className="flex items-center justify-between mb-2">
                        <h3 className="text-sm font-semibold text-gray-700">
                          Allowed Tools
                          <span className="ml-2 text-xs font-normal text-gray-500">
                            {editingTools 
                              ? `(${tempAllowedTools.length} selected)`
                              : selectedAgent.custom_config?.allowed_tools
                                ? `(${selectedAgent.tools.length} configured)`
                                : '(all tools)'
                            }
                          </span>
                        </h3>
                        {editingTools ? (
                          <div className="flex gap-2">
                            <button
                              onClick={handleSaveTools}
                              className="flex items-center px-2 py-1 text-xs bg-green-500 text-white rounded hover:bg-green-600"
                            >
                              <Save className="w-3 h-3 mr-1" />
                              Save
                            </button>
                            <button
                              onClick={() => setEditingTools(false)}
                              className="flex items-center px-2 py-1 text-xs bg-gray-500 text-white rounded hover:bg-gray-600"
                            >
                              <X className="w-3 h-3 mr-1" />
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={handleStartEditTools}
                            className="flex items-center px-2 py-1 text-xs bg-indigo-500 text-white rounded hover:bg-indigo-600"
                          >
                            <Edit2 className="w-3 h-3 mr-1" />
                            Configure
                          </button>
                        )}
                      </div>
                      
                      {editingTools ? (
                        <div className="bg-white p-3 rounded border border-gray-200 space-y-4">
                          <p className="text-xs text-gray-500">
                            Click to toggle. Selected tools will be available to this agent.
                          </p>
                          
                          {/* Get filtered tools for this agent */}
                          {(() => {
                            const filteredTools = getAvailableToolsForAgent(selectedAgent.name, selectedAgent.category);
                            const nativeTools = filteredTools.filter(t => t.source === 'native');
                            const mcpTools = filteredTools.filter(t => t.source === 'mcp');
                            
                            return (
                              <>
                                {/* Native Tools */}
                                <div>
                                  <div className="flex items-center gap-1.5 mb-2">
                                    <Cpu className="w-3.5 h-3.5 text-orange-500" />
                                    <h4 className="text-xs font-medium text-gray-700">Native Tools</h4>
                                    <span className="text-[10px] text-gray-400">
                                      ({nativeTools.length})
                                    </span>
                                  </div>
                                  <div className="flex flex-wrap gap-1.5">
                                    {nativeTools.map(tool => {
                                      const isSelected = tempAllowedTools.includes(tool.name);
                                      return (
                                        <button
                                          key={tool.name}
                                          onClick={() => handleToggleTool(tool.name)}
                                          className={`px-2 py-1 text-xs rounded border transition-colors ${
                                            isSelected
                                              ? 'bg-green-50 border-green-300 text-green-700 hover:bg-green-100'
                                              : 'bg-gray-50 border-gray-300 text-gray-400 hover:bg-gray-100'
                                          }`}
                                          title={tool.description}
                                        >
                                          {tool.name}
                                        </button>
                                      );
                                    })}
                                  </div>
                                </div>
                                
                                {/* MCP Tools */}
                                {mcpTools.length > 0 && (
                                  <div>
                                    <div className="flex items-center gap-1.5 mb-2">
                                      <Server className="w-3.5 h-3.5 text-green-500" />
                                      <h4 className="text-xs font-medium text-gray-700">MCP Tools</h4>
                                      <span className="text-[10px] text-gray-400">
                                        ({mcpTools.length})
                                      </span>
                                    </div>
                                    <div className="flex flex-wrap gap-1.5">
                                      {mcpTools.map(tool => {
                                        const isSelected = tempAllowedTools.includes(tool.name);
                                        return (
                                          <button
                                            key={tool.name}
                                            onClick={() => handleToggleTool(tool.name)}
                                            className={`px-2 py-1 text-xs rounded border transition-colors ${
                                              isSelected
                                                ? 'bg-green-50 border-green-300 text-green-700 hover:bg-green-100'
                                                : 'bg-gray-50 border-gray-300 text-gray-400 hover:bg-gray-100'
                                            }`}
                                            title={tool.description}
                                          >
                                            {tool.name}
                                          </button>
                                        );
                                      })}
                                    </div>
                                  </div>
                                )}
                              </>
                            );
                          })()}
                          
                          {/* Skills */}
                          <div>
                            <div className="flex items-center gap-1.5 mb-2">
                              <Sparkles className="w-3.5 h-3.5 text-purple-500" />
                              <h4 className="text-xs font-medium text-gray-700">Skills</h4>
                              <span className="text-[10px] text-gray-400">
                                ({skills.length})
                              </span>
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                              {skills.map(skill => {
                                const skillToolName = `skill:${skill.name}`;
                                const isSelected = tempAllowedTools.includes(skillToolName);
                                return (
                                  <button
                                    key={skill.name}
                                    onClick={() => handleToggleTool(skillToolName)}
                                    className={`px-2 py-1 text-xs rounded border transition-colors ${
                                      isSelected
                                        ? 'bg-green-50 border-green-300 text-green-700 hover:bg-green-100'
                                        : 'bg-gray-50 border-gray-300 text-gray-400 hover:bg-gray-100'
                                    }`}
                                    title={skill.description}
                                  >
                                    {skill.name}
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        </div>
                      ) : (
                        <div className="bg-white p-3 rounded border border-gray-200">
                          {selectedAgent.custom_config?.allowed_tools ? (
                            <div className="space-y-3">
                              {/* Show native tools */}
                              {selectedAgent.tools.filter(t => !t.includes('__') && !t.startsWith('skill:')).length > 0 && (
                                <div>
                                  <div className="flex items-center gap-1.5 mb-1.5">
                                    <Cpu className="w-3 h-3 text-orange-500" />
                                    <span className="text-[10px] font-medium text-gray-500">Native</span>
                                  </div>
                                  <div className="flex flex-wrap gap-1">
                                    {selectedAgent.tools.filter(t => !t.includes('__') && !t.startsWith('skill:')).map(toolName => (
                                      <span
                                        key={toolName}
                                        className="px-1.5 py-0.5 text-[10px] bg-green-50 border border-green-200 text-green-700 rounded"
                                      >
                                        {toolName}
                                      </span>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {/* Show MCP tools */}
                              {selectedAgent.tools.filter(t => t.includes('__')).length > 0 && (
                                <div>
                                  <div className="flex items-center gap-1.5 mb-1.5">
                                    <Server className="w-3 h-3 text-green-500" />
                                    <span className="text-[10px] font-medium text-gray-500">MCP</span>
                                  </div>
                                  <div className="flex flex-wrap gap-1">
                                    {selectedAgent.tools.filter(t => t.includes('__')).map(toolName => (
                                      <span
                                        key={toolName}
                                        className="px-1.5 py-0.5 text-[10px] bg-green-50 border border-green-200 text-green-700 rounded"
                                      >
                                        {toolName.split('__').pop()}
                                      </span>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {/* Show skills */}
                              {selectedAgent.tools.filter(t => t.startsWith('skill:')).length > 0 && (
                                <div>
                                  <div className="flex items-center gap-1.5 mb-1.5">
                                    <Sparkles className="w-3 h-3 text-purple-500" />
                                    <span className="text-[10px] font-medium text-gray-500">Skills</span>
                                  </div>
                                  <div className="flex flex-wrap gap-1">
                                    {selectedAgent.tools.filter(t => t.startsWith('skill:')).map(toolName => (
                                      <span
                                        key={toolName}
                                        className="px-1.5 py-0.5 text-[10px] bg-green-50 border border-green-200 text-green-700 rounded"
                                      >
                                        {toolName.replace('skill:', '')}
                                      </span>
                                    ))}
                                  </div>
                                </div>
                              )}
                            </div>
                          ) : (
                            <p className="text-xs text-gray-500">
                              All tools available (click Configure to customize)
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-center py-12 text-gray-500">
                  <Bot className="w-12 h-12 mx-auto mb-4 opacity-50" />
                  <p>Select an agent to view details</p>
                </div>
              )}
            </div>
          </>
        )}

        {showCreateAgent && (
          <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
              <div className="p-6">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-lg font-semibold text-gray-900">Create Agent</h2>
                  <button
                    onClick={() => setShowCreateAgent(false)}
                    className="p-1 text-gray-500 hover:text-gray-700"
                  >
                    <X className="w-5 h-5" />
                  </button>
                </div>
                
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Name *</label>
                    <input
                      type="text"
                      value={newAgent.name}
                      onChange={(e) => setNewAgent({ ...newAgent, name: e.target.value })}
                      className="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                      placeholder="e.g., reviewer, coder"
                    />
                  </div>
                  
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Description *</label>
                    <input
                      type="text"
                      value={newAgent.description}
                      onChange={(e) => setNewAgent({ ...newAgent, description: e.target.value })}
                      className="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                      placeholder="What this agent does"
                    />
                  </div>
                  
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">System Prompt *</label>
                    <textarea
                      value={newAgent.system_prompt}
                      onChange={(e) => setNewAgent({ ...newAgent, system_prompt: e.target.value })}
                      className="w-full h-40 px-3 py-2 text-sm font-mono border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500 resize-none"
                      placeholder="Enter the system prompt..."
                    />
                  </div>
                  
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Model (optional)</label>
                    <select
                      value={newAgent.model}
                      onChange={(e) => setNewAgent({ ...newAgent, model: e.target.value })}
                      className="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                    >
                      <option value="">Use default model</option>
                      {getAllModels().map(m => (
                        <option key={m.id} value={m.id}>
                          {m.name} ({m.provider})
                        </option>
                      ))}
                    </select>
                  </div>
                  
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Allowed Tools</label>
                    <p className="text-xs text-gray-500 mb-2">
                      Leave empty for all tools. Click to toggle.
                    </p>
                    <div className="flex flex-wrap gap-1 max-h-32 overflow-y-auto">
                      {allToolsForConfig.map(tool => {
                        const isEnabled = newAgent.allowed_tools.length === 0 || newAgent.allowed_tools.includes(tool.name);
                        return (
                          <button
                            key={tool.name}
                            onClick={() => {
                              const newTools = newAgent.allowed_tools.includes(tool.name)
                                ? newAgent.allowed_tools.filter(t => t !== tool.name)
                                : [...newAgent.allowed_tools, tool.name];
                              setNewAgent({ ...newAgent, allowed_tools: newTools });
                            }}
                            className={`px-2 py-1 text-xs rounded border transition-colors ${
                              isEnabled
                                ? 'bg-green-50 border-green-300 text-green-700 hover:bg-green-100'
                                : 'bg-gray-50 border-gray-300 text-gray-500 hover:bg-gray-100'
                            }`}
                            title={tool.description}
                          >
                            {tool.name}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
                
                <div className="flex justify-end gap-2 mt-6">
                  <button
                    onClick={() => setShowCreateAgent(false)}
                    className="px-4 py-2 text-sm text-gray-700 bg-gray-100 rounded hover:bg-gray-200"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleCreateAgent}
                    className="px-4 py-2 text-sm text-white bg-indigo-500 rounded hover:bg-indigo-600"
                  >
                    Create
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </PageLayout>
  );
}
