/* Copyright (C) 2012 Ion Torrent Systems, Inc. All Rights Reserved */

#ifndef EMPTYTRACE_H
#define EMPTYTRACE_H

#include "BkgTrace.h"
#include "PinnedInFlow.h"
#include "Mask.h"
#include "CommandLineOpts.h"
#include "SynchDat.h"
#include "Serialization.h"

class EmptyTrace
{
  public:
    EmptyTrace ( CommandLineOpts &clo );
    virtual ~EmptyTrace();
    virtual void GenerateAverageEmptyTrace ( Region *region, PinnedInFlow &pinnedInFlow, Mask *bfmask, Image *img, int flow );
    virtual void GenerateAverageEmptyTrace ( Region *region, PinnedInFlow& pinnedInFlow, Mask *bfmask, SynchDat &sdat, int flow );
    virtual void GenerateAverageEmptyTraceUncomp ( TimeCompression &time_cp, Region *region, PinnedInFlow& pinnedInFlow, Mask *bfmask, SynchDat &sdat, int flow );
    virtual void  Allocate ( int numfb, int _imgFrames );
    void  PrecomputeBackgroundSlopeForDeriv ( int iFlowBuffer );
    void  FillEmptyTraceFromBuffer ( short *bkg, int flow );
    void  RezeroReference ( float t_start, float t_end, int fnum );
    void  RezeroReferenceAllFlows ( float t_start, float t_end );
    void  GetShiftedBkg ( float tshift,TimeCompression &time_cp, float *bkg );
    void  GetShiftedSlope ( float tshift, TimeCompression &time_cp, float *bkg );
    void  T0EstimateToMap ( std::vector<float>& sep_t0_est, Region *region, Mask *bfmask );
    int CountReferenceTraces ( Region& region, Mask *bfmask );

    void Dump_bg_buffers ( char *ss, int start, int len ); //JGV
    void DumpEmptyTrace ( FILE *fp, int x, int y );

    int imgFrames;

    float *bg_buffers;  // should be private, exposed in the HDF5 dump
    // float * get_bg_buffers(int flowBufferReadPos) { return &bg_buffers[flowBufferReadPos*imgFrames]; }
    float *bg_dc_offset;

    int regionIndex;
    int nOutliers;
    int nRef;

    void SetTrimWildTraceOptions ( bool _do_ref_trace_trim,
                                   float _span_inflator_min,
                                   float _span_inflator_mult,
                                   float _cutoff_quantile,
                                   float _nuc_flow_frame_width );

    void SetTime(float frames_per_second);
    void SetTimeFromSdatChunk(Region& region, SynchDat &sdat);

    // track whether this emptytrace object is used, if it is not then
    // the buffers may be allocated but not contain valid data
    bool SetUsed(bool arg) { trace_used=arg; return trace_used; }
    bool GetUsed() { return trace_used; }

  protected:
    int numfb; // number of flow buffers 
    float t0_mean;
    std::vector<int> regionIndices;
    float *neg_bg_buffers_slope;
    std::vector<float> t0_map;  //@TODO: timing map across all regions
    std::vector<float> timePoints; // timing for start and stop of acquisition time for frame.
    float secondsPerFrame;
    MaskType referenceMask;
    BkgTrace zeroTrace;
    // emptyTrace outlier (wild trace) removal
    bool do_ref_trace_trim;
    float span_inflator_min;
    float span_inflator_mult;
    float cutoff_quantile;
    float nuc_flow_frame_width;
    std::vector<int> sampleIndex;
    bool trace_used;

//@TODO: this is not a safe way of accessing buffers - need flow buffer object reference
    int flowToBuffer ( int flow )
    {
      return ( flow % numfb );  // mapping of flow to bg_buffer entry
    };

    // kernel used to smooth and measure the slope of the background signal
    // this >never< changes, so is fine as a static const variable
#define BKG_SGSLOPE_LEN 5
    static const float bkg_sg_slope[BKG_SGSLOPE_LEN];
    void  SavitskyGolayComputeSlope ( float *local_slope,float *source_val, int len );

    float ComputeDcOffsetEmpty ( float *bPtr, float t_start, float t_end );

    void  AccumulateEmptyTrace ( float *bPtr, float *tmp_shifted, float w );
    void  ShiftMe ( float tshift, TimeCompression &time_cp, float *my_buff, float *out_buff );
    bool ReferenceWell ( int ax, int ay, Mask *bfmask );
    void RemoveEmptyTrace ( float *bPtr, float *tmp_shifted, float w );
    float TrimWildTraces ( Region *region, float *bPtr, std::vector<float>& valsAtT0,
                           std::vector<float>& valsAtT1,
                           std::vector<float>& valsAtT2, float total_weight,
                           Mask *bfmask, Image *img, SynchDat *sdat );
    int SecondsToIndex ( float seconds, std::vector<float>& delta );

  private:
    void AllocateScratch();

    EmptyTrace();   // do not use

    friend class boost::serialization::access;
    template<typename Archive>
    void save ( Archive& ar, const unsigned int version ) const
    {
      //fprintf(stdout, "Serialization: save EmptyTrace ... ");
      ar &
      imgFrames &
      regionIndex &
      nOutliers &
      numfb &
      t0_mean &
      nRef &
      regionIndices &
      t0_map &
      timePoints &
      secondsPerFrame &
      referenceMask &
      do_ref_trace_trim &
      span_inflator_min &
      span_inflator_mult &
      cutoff_quantile &
      nuc_flow_frame_width &
      sampleIndex &
      trace_used;

      //fprintf(stdout, "done\n");
    }
    template<typename Archive>
    void load ( Archive& ar, const unsigned int version )
    {
      // fprintf(stdout, "Serialization: load EmptyTrace ... ");
      ar &
      imgFrames &
      regionIndex &
      nOutliers &
      numfb &
      t0_mean &
      nRef &
      regionIndices &
      t0_map &
      timePoints &
      secondsPerFrame &
      referenceMask &
      do_ref_trace_trim &
      span_inflator_min &
      span_inflator_mult &
      cutoff_quantile &
      nuc_flow_frame_width &
      sampleIndex &
      trace_used;

      AllocateScratch();
      // fprintf(stdout, "done\n");
    }
    BOOST_SERIALIZATION_SPLIT_MEMBER()

};



inline bool EmptyTrace::ReferenceWell ( int ax, int ay, Mask *bfmask )
{
  // is this well a valid reference coming out of beadfind?
  bool isReference = bfmask->Match ( ax,ay,referenceMask );
  bool isIgnoreOrAmbig = bfmask->Match ( ax,ay, ( MaskType ) ( MaskIgnore ) );
  bool isUnpinned = ! bfmask->Match ( ax,ay,MaskPinned );

  return ( isReference && isUnpinned && !isIgnoreOrAmbig );
}

#endif // EMPTYTRACE_H
