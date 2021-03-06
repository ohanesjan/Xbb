#!/usr/bin/env python
from __future__ import print_function
from optparse import OptionParser
import ROOT
ROOT.gROOT.SetBatch(True)
from myutils import NewTreeCache as TreeCache
from myutils.sampleTree import SampleTree as SampleTree
from myutils import BetterConfigParser, ParseInfo
import resource
import os
import sys
import pickle
import glob
import shutil
import numpy as np
import math
from copy import deepcopy
import gzip

class SampleTreesToNumpyConverter(object):

    def __init__(self, config, mvaName, useSyst=True, useWeightSyst=True):
        self.mvaName = mvaName
        VHbbNameSpace = config.get('VHbbNameSpace', 'library')
        ROOT.gSystem.Load(VHbbNameSpace)
        self.dataFormatVersion = 2
        self.sampleTrees = []
        self.config = config
        self.samplesPath = config.get('Directories', 'MVAin')
        self.samplesDefinitions = config.get('Directories','samplesinfo')
        self.samplesInfo = ParseInfo(self.samplesDefinitions, self.samplesPath)

        # region
        self.treeCutName = config.get(mvaName, 'treeCut')
        self.treeCut = config.get('Cuts', self.treeCutName)

        # split in train/eval sets
        self.trainCut = config.get('Cuts', 'TrainCut') 
        self.evalCut = config.get('Cuts', 'EvalCut')
        # rescale MC by 2 because of train/eval split
        self.globalRescale = 2.0

        # variables and systematics
        self.treeVarSet = config.get(mvaName, 'treeVarSet')
        self.MVA_Vars = {'Nominal': [x for x in config.get(self.treeVarSet, 'Nominal').strip().split(' ') if len(x.strip()) > 0]}

        self.weightSYS = []
        self.weightSYSweights = {}

        if useSyst:
            print ("including systematics")
            self.systematics = config.get('systematics', 'systematics').strip().split(' ')
            
            for sys in self.systematics:
                self.MVA_Vars[sys] = [x for x in config.get(self.treeVarSet, sys).strip().split(' ') if len(x.strip()) > 0]
        else:
            self.systematics = []
        print ("systematics: "+", ".join(self.systematics))

        if useWeightSyst:
            print ("including Btag weight systematics")
            self.weightWithoutBtag = self.config.get('Weights','weight_noBTag')
            self.bTagWeight = self.config.get('Weights','bTagWeight')
            
            for d in ['Up','Down']:
                for syst in ['HFStats1','HFStats2','LF','HF','LFStats1','LFStats2','cErr2','cErr1','JES']:
                  for npt in ["0","1","2","3"]:
                    for neta in ["1","2","3"]:
			#splitted systematics
                        systFullName = "btag_" + syst + "_pt"+ npt + "_eta" + neta + "_" + d
                        weightName = "bTagWeightCMVAV2_Moriond_" +  syst + "_pt"+ npt + "_eta" + neta  + d
			
			#not splitted systematics			
#                    systFullName = "btag_" + syst + "_" + d
#                    weightName = self.bTagWeight + "_" +  syst + d
                    self.weightSYSweights[systFullName] = self.weightWithoutBtag + '*' + weightName
                    self.weightSYS.append(systFullName)
        print ("btag weights: "+", ".join(self.weightSYS))


        # samples
        self.sampleNames = {
#                   'BKG_TT': eval(self.config.get('Plot_general', 'TT')),
#                   'BKG_ST': eval(self.config.get('Plot_general', 'ST')),
#                   'BKG_VV': eval(self.config.get('Plot_general', 'VV')),
#                   'BKG_DY2b': eval(self.config.get('Plot_general', 'DY2b')),
#                   'BKG_DY1b': eval(self.config.get('Plot_general', 'DY1b')),
#                   'BKG_DY0b': eval(self.config.get('Plot_general', 'DYlight')),
#                   'SIG_ggZH': eval(self.config.get('Plot_general', 'ggZH')),
#                   'SIG_qqZH': eval(self.config.get('Plot_general', 'qqZH')),
                    'SIG_ALL': eval(self.config.get('Plot_general', 'allSIG')),
                    'BKG_ALL': eval(self.config.get('Plot_general', 'allBKG')),
                }
        if self.config.has_option(mvaName, 'classDict'):
            self.sampleNames = eval(self.config.get(mvaName, 'classDict'))
            print("classes dict:", self.sampleNames)
        self.samples = {category: self.samplesInfo.get_samples(samples) for category,samples in self.sampleNames.iteritems()}


    def run(self):
        # ----------------------------------------------------------------------------------------------------------------------
        # add sig/bkg x training/testing trees
        # ----------------------------------------------------------------------------------------------------------------------
        categories = self.samples.keys()
        datasetParts = {'train': self.trainCut, 'test': self.evalCut}

        systematics = self.systematics
        arrayLists = {datasetName:[] for datasetName in datasetParts.iterkeys()}
        arrayLists_sys = {x: {datasetName:[] for datasetName in datasetParts.iterkeys()} for x in systematics}
        weightLists = {datasetName:[] for datasetName in datasetParts.iterkeys()}
        targetLists = {datasetName:[] for datasetName in datasetParts.iterkeys()}

        weightListsSYS = {x: {datasetName:[] for datasetName in datasetParts.iterkeys()} for x in self.weightSYS} 
        
        # standard weight expression
        weightF = self.config.get('Weights','weightF')

        for category in categories:
            for sample in self.samples[category]:
                print ('*'*80,'\n%s\n'%sample,'*'*80)
                for datasetName, additionalCut in datasetParts.iteritems():
                    # cuts
                    sampleCuts = [sample.subcut]
                    if additionalCut:
                        sampleCuts.append(additionalCut)
                    # cut from the mva region
                    if self.treeCut:
                        sampleCuts.append(self.treeCut)

                    # get ROOT tree for selected sample & region cut
                    tc = TreeCache.TreeCache(
                            sample=sample,
                            cutList=sampleCuts,
                            inputFolder=self.samplesPath,
                            config=self.config,
                            debug=True
                        )
                    sampleTree = tc.getTree()
                    if sampleTree:
                        treeScale = sampleTree.getScale(sample) * self.globalRescale
                        print ('scale:', treeScale)
                        
                        # initialize numpy array
                        nSamples = sampleTree.GetEntries()
                        features = self.MVA_Vars['Nominal']
                        features_sys = {x: self.MVA_Vars[x] for x in systematics} 
                        nFeatures = len(features) 
                        print('nFeatures:', nFeatures)
                        inputData = np.zeros((nSamples, nFeatures), dtype=np.float32)
                        inputData_sys = {x: np.zeros((nSamples, nFeatures), dtype=np.float32) for x in systematics}

                        # initialize formulas for ROOT tree
                        for feature in features:
                            sampleTree.addFormula(feature)
                        for k, features_s in features_sys.iteritems():
                            for feature in features_s:
                                sampleTree.addFormula(feature)
                        sampleTree.addFormula(weightF)
                        for syst in self.weightSYS:
                            sampleTree.addFormula(self.weightSYSweights[syst])
                        
                        # fill numpy array from ROOT tree
                        for i, event in enumerate(sampleTree):
                            for j, feature in enumerate(features):
                                inputData[i, j] = sampleTree.evaluate(feature)
                            # total weight comes from weightF (btag, lepton sf, ...) and treeScale to scale MC to x-section
                            totalWeight = treeScale * sampleTree.evaluate(weightF)
                            weightLists[datasetName].append(totalWeight)
                            targetLists[datasetName].append(categories.index(category))
                            
                            # add weights varied by (btag) systematics
                            for syst in self.weightSYS:
                                weightListsSYS[syst][datasetName].append(treeScale * sampleTree.evaluate(self.weightSYSweights[syst]))

                            # fill systematics 
                            for k, feature_s in features_sys.iteritems():
                                for j, feature in enumerate(feature_s):
                                    inputData_sys[k][i,j] = sampleTree.evaluate(feature)

                        arrayLists[datasetName].append(inputData)
                        for sys in systematics:
                            arrayLists_sys[sys][datasetName].append(inputData_sys[sys])

                    else:
                        print ("\x1b[31mERROR: TREE NOT FOUND:", sample.name, " -> not cached??\x1b[0m")
                        raise Exception("CachedTreeMissing")

       #systematics for training
        puresystematics = deepcopy(systematics)
        if 'Nominal' in puresystematics:
            puresystematics.remove('Nominal')

        # concatenate all data from different samples
        self.data = {
                'train': {
                    'X': np.concatenate(arrayLists['train'], axis=0),
                    'y': np.array(targetLists['train'], dtype=np.float32),
                    'sample_weight': np.array(weightLists['train'], dtype=np.float32),
                    },
                'test': {
                    'X': np.concatenate(arrayLists['test'], axis=0), 
                    'y': np.array(targetLists['test'], dtype=np.float32), 
                    'sample_weight': np.array(weightLists['test'], dtype=np.float32),
                    },
                'category_labels': {idx: label for idx, label in enumerate(categories)},
                'meta': {
                    'version': self.dataFormatVersion,
                    'region': self.mvaName,
                    'cutName': self.treeCutName,
                    'cut': self.treeCut,
                    'trainCut': self.trainCut,
                    'testCut': self.evalCut,
                    'samples': self.sampleNames,
                    'weightF': weightF,
                    'weightSYS': self.weightSYS,
                    'variables': ' '.join(self.MVA_Vars['Nominal']),
                    'systematics': puresystematics,
                    }
                }
        # add systematics variations
        for sys in systematics:
            self.data['train']['X_'+sys] = np.concatenate(arrayLists_sys[sys]['train'], axis=0)
        for syst in self.weightSYS:
            self.data['train']['sample_weight_'+syst] = np.array(weightListsSYS[syst]['train'], dtype=np.float32)
        if not os.path.exists("./dumps"):
                os.makedirs("dumps")
        numpyOutputFileName = './dumps/' +self.config.get("Directories","Dname").split("_")[1] + '_' + self.mvaName + '.dmpz'
        with gzip.open(numpyOutputFileName, 'wb') as outputFile:
            pickle.dump(self.data, outputFile)
        print(self.data['meta'])
        print("written to:\x1b[34m", numpyOutputFileName, " \x1b[0m")

# read arguments
argv = sys.argv
parser = OptionParser()
parser.add_option("-C", "--config", dest="config", default=[], action="append",
                      help="configuration file")
parser.add_option("-T", "--tag", dest="tag", default='',
                      help="configuration tag")
parser.add_option("-t","--trainingRegions", dest="trainingRegions", default='',
                      help="cut region identifier")
parser.add_option("-S","--systematics", dest="systematics", default=2,
                      help="include systematics (0 for none, 1 for bdtVars, 2 for all (with btagWeights)")
(opts, args) = parser.parse_args(argv)
if opts.config =="":
        opts.config = ["config"]

# Import after configure to get help message
from myutils import BetterConfigParser, mvainfo, ParseInfo

if len(opts.tag.strip()) > 1:
    config = BetterConfigParser()
    config.read("{tag}config/paths.ini".format(tag=opts.tag))
    configFiles = config.get("Configuration", "List").split(' ')
    opts.config = ["{tag}config/{file}".format(tag=opts.tag, file=x.strip()) for x in configFiles]
    print("reading config files:", opts.config)

sys = False
btagSys = False
if int(opts.systematics) > 0:
    sys = True
    if int(opts.systematics) > 1:
        btagSys = True
# load config
config = BetterConfigParser()
config.read(opts.config)
converter = SampleTreesToNumpyConverter(config, opts.trainingRegions, useSyst=sys, useWeightSyst=btagSys)
converter.run()
