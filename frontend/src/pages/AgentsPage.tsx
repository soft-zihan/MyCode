import { useState, useEffect } from 'react';
import { 
  fetchAgents, fetchAgent, fetchConfig, saveConfig, 
  Agent, AgentDetail, AppConfig, ModelEndpointConfig 
} from '../api/client';
import { Bot, Server, RefreshCw, Wrench, Save, Plus, Trash2, ChevronDown, ChevronRight, Globe, Cpu, Search, CheckCircle, XCircle, Loader } from 'lucide-react';

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
}

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<AgentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [activeTab, setActiveTab] = useState<'models' | 'agents'>('models');
  const [providers, setProviders] = useState<ApiProvider[]>([]);
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [verifyingModel, setVerifyingModel] = useState<string | null>(null);
  const [verifyResults, setVerifyResults] = useState<Record<string, { status: string; message: string }>>({});

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [agentsData, configData] = await Promise.all([
        fetchAgents(),
        fetchConfig(),
      ]);
      setAgents(agentsData);
      setConfig(configData);
      
      // Convert endpoints to providers
      const providerMap = new Map<string, ApiProvider>();
      Object.entries(configData.endpoints).forEach(([id, endpoint]) => {
        const key = `${endpoint.base_url}|${endpoint.api_key}`;
        if (!providerMap.has(key)) {
          providerMap.set(key, {
            id: `provider_${providerMap.size}`,
            name: new URL(endpoint.base_url).hostname,
            base_url: endpoint.base_url,
            api_key: endpoint.api_key,
            models: []
          });
        }
        const provider = providerMap.get(key)!;
        provider.models.push({
          id: id,
          name: endpoint.model,
          context_window: endpoint.context_window || 128000
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
    try {
      const detail = await fetchAgent(agentName);
      setSelectedAgent(detail);
    } catch (err) {
      console.error('Failed to load agent detail:', err);
    }
  };

  const handleSaveConfig = async () => {
    if (!config) return;
    setSaving(true);
    try {
      // Convert providers back to endpoints
      const endpoints: Record<string, ModelEndpointConfig> = {};
      providers.forEach(provider => {
        provider.models.forEach(model => {
          endpoints[model.id] = {
            model: model.name,
            base_url: provider.base_url,
            api_key: provider.api_key,
            context_window: model.context_window
          };
        });
      });
      
      await saveConfig({ ...config, endpoints });
      alert('Config saved successfully!');
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
            context_window: 128000
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
        <div className="text-gray-500">Loading agents...</div>
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

  return (
    <div className="h-full flex flex-col bg-white">
      <div className="p-6 border-b border-gray-200">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Agents & Models</h1>
            <p className="text-sm text-gray-500 mt-1">
              {agents.length} agent{agents.length !== 1 ? 's' : ''} • {providers.length} provider{providers.length !== 1 ? 's' : ''} • {getAllModels().length} model{getAllModels().length !== 1 ? 's' : ''}
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
              className="flex items-center px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors disabled:opacity-50"
            >
              <Save className="w-4 h-4 mr-2" />
              {saving ? 'Saving...' : 'Save Config'}
            </button>
          </div>
        </div>

        <div className="flex gap-4 border-b border-gray-200">
          <button
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === 'models'
                ? 'text-blue-600 border-b-2 border-blue-600'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveTab('models')}
          >
            <Server className="w-4 h-4 inline mr-2" />
            Model Providers
          </button>
          <button
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === 'agents'
                ? 'text-blue-600 border-b-2 border-blue-600'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveTab('agents')}
          >
            <Bot className="w-4 h-4 inline mr-2" />
            Agents ({agents.length})
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {activeTab === 'models' ? (
          <div className="p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-gray-900">API Providers</h2>
              <button
                onClick={addProvider}
                className="flex items-center px-3 py-1.5 text-sm bg-green-500 text-white rounded hover:bg-green-600 transition-colors"
              >
                <Plus className="w-4 h-4 mr-1" />
                Add Provider
              </button>
            </div>

            {providers.length === 0 ? (
              <div className="text-center py-12 text-gray-500">
                <Server className="w-12 h-12 mx-auto mb-4 opacity-50" />
                <p>No API providers configured</p>
                <p className="text-sm mt-2">Add an OpenAI-compatible API provider to get started</p>
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
                        <Globe className="w-5 h-5 text-blue-500" />
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
                              className="w-full px-3 py-2 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
                              placeholder="e.g., OpenAI, DeepSeek"
                            />
                          </div>
                          <div>
                            <label className="block text-xs font-medium text-gray-600 mb-1">Base URL</label>
                            <input
                              type="text"
                              value={provider.base_url}
                              onChange={(e) => updateProvider(provider.id, 'base_url', e.target.value)}
                              className="w-full px-3 py-2 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
                              placeholder="https://api.openai.com/v1"
                            />
                          </div>
                          <div>
                            <label className="block text-xs font-medium text-gray-600 mb-1">API Key</label>
                            <input
                              type="password"
                              value={provider.api_key}
                              onChange={(e) => updateProvider(provider.id, 'api_key', e.target.value)}
                              className="w-full px-3 py-2 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
                              placeholder="sk-..."
                            />
                          </div>
                        </div>

                        <div className="border-t border-gray-200 pt-4">
                          <div className="flex items-center justify-between mb-3">
                            <h4 className="text-sm font-semibold text-gray-700">Models</h4>
                            <button
                              onClick={() => addModel(provider.id)}
                              className="flex items-center px-2 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600"
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
                                      className="flex-1 px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
                                      placeholder="Model name"
                                    />
                                    <input
                                      type="number"
                                      value={model.context_window}
                                      onChange={(e) => updateModel(provider.id, model.id, 'context_window', parseInt(e.target.value) || 128000)}
                                      className="w-24 px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
                                      placeholder="Context"
                                      title="Context window size"
                                    />
                                    <span className="text-xs text-gray-500">tokens</span>
                                    <button
                                      onClick={() => verifyModel(provider.id, model.id)}
                                      disabled={verifyingModel === model.id}
                                      className="flex items-center px-2 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed"
                                      title="Verify model"
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
                                        : verifyResults[model.id].status === 'warning'
                                        ? 'bg-yellow-50 text-yellow-800 border border-yellow-200'
                                        : 'bg-red-50 text-red-800 border border-red-200'
                                    }`}>
                                      {verifyResults[model.id].status === 'success' ? (
                                        <CheckCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                                      ) : verifyResults[model.id].status === 'warning' ? (
                                        <XCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-yellow-600" />
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
              </div>
            )}
          </div>
        ) : (
          <div className="p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-4">Agent Configuration</h2>

            {/* Side Query Model Selection */}
            <div className="mb-6 p-4 bg-gradient-to-r from-indigo-50 to-blue-50 rounded-lg border border-indigo-200">
              <div className="flex items-center gap-2 mb-2">
                <Search className="w-5 h-5 text-indigo-600" />
                <h3 className="text-sm font-semibold text-indigo-900">Side Query Model</h3>
              </div>
              <p className="text-xs text-indigo-700 mb-3">
                This model is used for background tasks like memory recall (semantic matching), 
                context summarization, and other auxiliary queries that run alongside the main agent.
              </p>
              <div className="flex items-center gap-2">
                <label className="text-xs font-medium text-gray-600">Model:</label>
                <select
                  value={config?.routing?.side || ''}
                  onChange={(e) => {
                    if (!config) return;
                    setConfig({
                      ...config,
                      routing: { ...config.routing, side: e.target.value }
                    });
                  }}
                  className="px-3 py-1.5 text-sm border border-indigo-300 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500 bg-white"
                >
                  <option value="">Not configured</option>
                  {getAllModels().map(m => (
                    <option key={m.id} value={m.id}>
                      {m.name} ({m.provider})
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="space-y-3">
              {agents.map(agent => (
                <div
                  key={agent.name}
                  onClick={() => handleAgentClick(agent.name)}
                  className={`p-4 rounded-lg border cursor-pointer transition-all ${
                    selectedAgent?.name === agent.name
                      ? 'border-blue-500 bg-blue-50'
                      : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50'
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-2">
                        <span className="font-semibold text-gray-900">{agent.name}</span>
                        {agent.is_custom && (
                          <span className="px-2 py-0.5 text-xs bg-purple-100 text-purple-700 rounded">
                            Custom
                          </span>
                        )}
                      </div>
                      <p className="text-sm text-gray-600 mb-3">{agent.description}</p>
                      
                      <div className="flex items-center gap-4">
                        <div className="flex items-center gap-2">
                          <label className="text-xs text-gray-500">Default Model:</label>
                          <select
                            value={config?.routing[agent.name] || ''}
                            onChange={(e) => {
                              e.stopPropagation();
                              updateAgentRouting(agent.name, e.target.value);
                            }}
                            onClick={(e) => e.stopPropagation()}
                            className="px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
                          >
                            <option value="">Not configured</option>
                            {getAllModels().map(m => (
                              <option key={m.id} value={m.id}>
                                {m.name} ({m.provider})
                              </option>
                            ))}
                          </select>
                        </div>
                        
                        {agent.allowed_tools && (
                          <span className="flex items-center text-xs text-gray-500">
                            <Wrench className="w-3 h-3 mr-1" />
                            {agent.allowed_tools.length} tools
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>

            {selectedAgent && (
              <div className="mt-6 p-4 bg-gray-50 rounded-lg border border-gray-200">
                <h3 className="text-sm font-semibold text-gray-900 mb-3">
                  Agent Detail: {selectedAgent.name}
                </h3>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div>
                    <span className="text-gray-500">Tools:</span>
                    <span className="ml-2 text-gray-900">{selectedAgent.tools_count}</span>
                  </div>
                  <div>
                    <span className="text-gray-500">Has System Prompt:</span>
                    <span className="ml-2 text-gray-900">{selectedAgent.has_system_prompt ? 'Yes' : 'No'}</span>
                  </div>
                </div>
                {selectedAgent.system_prompt_preview && (
                  <div className="mt-3">
                    <div className="text-xs text-gray-500 mb-1">System Prompt Preview:</div>
                    <pre className="text-xs bg-white p-2 rounded border border-gray-200 overflow-x-auto">
                      {selectedAgent.system_prompt_preview.slice(0, 500)}...
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
